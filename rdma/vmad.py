from __future__ import with_statement;

import collections;
import mmap;
import os;
import select;
import rdma;
import rdma.devices;
import rdma.IBA as IBA;
import rdma.ibverbs as ibv;

class BufferPool(object):
    """Hold onto a block of fixed size buffers and provide some helpers for
    using them as send and recv buffers with a QP."""
    #: Constant value to set wr_id to when it is not being used.
    NO_WR_ID = 0xFFFFFFFF;
    _mr = None;
    _mem = None;
    #: `deque` of buffer indexes
    _buffers = None;
    size = 0;
    count = 0;

    def __init__(self,pd,count,size):
        """Create a :class:`rdma.ibverbs.MR` in *pd* with *count* buffers of
        *size* bytes."""
        self.count = count;
        self.size = size;
        self._mem = mmap.mmap(-1,count*size);
        self._mr = pd.mr(self._mem,ibv.IBV_ACCESS_LOCAL_WRITE |
                         ibv.IBV_ACCESS_LOCAL_WRITE);
        self._buffers = collections.deque(xrange(count),count);

    def close(self):
        if self._mr is not None:
            self._mr.close();
        if self._mem is not None:
            self._mem.close();

    def post_recvs(self,qp,count):
        """Post *count* buffers for receive to \*qp."""
        if count == 0:
            return;

        wr = [];
        for I in range(count):
            buf_idx = self._buffers.pop();
            wr.append(ibv.recv_wr(wr_id=buf_idx,
                                  sg_list=self.make_sge(buf_idx,self.size)));
        qp.post_recv(wr);

    def finish_wcs(self,qp,wcs):
        """Process work completion list *wcs* to recover buffers attached to
        completed work and re-post recv buffers to qp. Every work request with
        an attached buffer must have a signaled completion to recover the buffer."""
        new_recvs = 0;
        for wc in wcs:
            if wc is None:
                continue;
            if wc.status != ibv.IBV_WC_SUCCESS:
                raise ibv.WCError(wc);
            if wc.opcode & ibv.IBV_WC_RECV:
                if wc.wr_id != self.NO_WR_ID:
                    self._buffers.append(wc.wr_id);
                new_recvs = new_recvs + 1;
            if (wc.opcode == ibv.IBV_WC_SEND or
                wc.opcode == ibv.IBV_WC_RDMA_WRITE or
                wc.opcode == ibv.IBV_WC_RDMA_READ):
                if wc.wr_id != self.NO_WR_ID:
                    self._buffers.append(wc.wr_id);
        return self.post_recvs(qp,new_recvs);

    def make_sge(self,buf_idx,buf_len):
        """Return a :class:`rdma.ibverbs.SGE` for *buf_idx*."""
        return ibv.sge(length=buf_len,lkey=self._mr.lkey,
                       addr=self._mr.addr + buf_idx*self.size);

    def copy_from(self,buf_idx,offset=0,length=0xFFFFFFF):
        """Return a copy of buffer *buf_idx*."""
        length = min(length,self.size - offset)
        return bytearray(self._mem[buf_idx*self.size + offset:
                                   buf_idx*self.size + offset + length]);

class VMAD(rdma.madtransactor.MADTransactor):
    '''Provide a UMAD style interface that runs on ibverbs. This can be
    used with QPN=1 traffic.'''
    #: :class:`rdma.devices.EndPort` this is associated with
    end_port = None;

    _pd = None;
    _cq = None;
    _ctx = None;
    _allocated_ctx = False;

    def __init__(self,parent,path,depth=16):
        """*path* is used to set the PKey and QKey for all MADs sent through
        this interface."""
        rdma.madtransactor.MADTransactor.__init__(self);
        self._tid = int(os.urandom(8).encode("hex"),16);

        if isinstance(parent,rdma.devices.EndPort):
            self._ctx = rdma.get_verbs(parent);
            self._allocated_ctx = True;
        elif isinstance(parent,ibv.Context):
            self._ctx = parent;
        self.end_port = self._ctx.end_port;

        self._cc = self._ctx.comp_channel();
        self._poll = select.poll();
        self._cc.register_poll(self._poll);
        self._cq = self._ctx.cq(2*depth,self._cc);
        self._cq.req_notify_cq();

        self._pd = self._ctx.pd();
        self._pool = BufferPool(self._pd,2*depth,256+40);
        self._qp = self._pd.qp(ibv.IBV_QPT_UD,depth,self._cq,
                               depth,self._cq);
        self._pool.post_recvs(self._qp,min(self._qp.max_recv_wr,depth));
        self._recvs = collections.deque();

        # Adjust the number of buffers so that we can't exceed the send q depth
        while len(self._pool._buffers) > self._qp.max_send_wr:
            self._pool._buffers.pop();

        path = path.copy(sqpn=self._qp.qp_num,sqpsn=self._tid&0xFFFFFF);
        self._qp.modify_to_init(path);
        self._qp.modify_to_rtr(path);
        self._qp.modify_to_rts(path);
        self.qkey = path.qkey;
        self.pkey = path.pkey;

    def sendto(self,buf,path):
        '''Send a MAD packet. *buf* is the raw MAD to send, starting with the first
        byte of :class:`rdma.IBA.MADHeader`. *path* is the destination.'''
        while not self._pool._buffers:
            self._cq_drain();
            if not self._pool._buffers:
                self._cq_sleep(None);

        if path.qkey != self.qkey or path.pkey != self.pkey:
            raise rdma.RDMAError("Destination %r does not match the qkey or pkey of this VMAD instance."%(path));

        buf_idx = self._pool._buffers.pop();
        size = self._pool.size;
        if isinstance(buf,bytearray):
            self._pool._mem[buf_idx*size:buf_idx*size + len(buf)] = bytes(buf);
        else:
            self._pool._mem[buf_idx*size:buf_idx*size + len(buf)] = buf;

        wr = ibv.send_wr(wr_id=buf_idx,
                         sg_list=self._pool.make_sge(buf_idx,len(buf)),
                         opcode=ibv.IBV_WR_SEND,
                         send_flags=ibv.IBV_SEND_SIGNALED,
                         ah=self._pd.ah(path),
                         remote_qpn=path.dqpn,
                         remote_qkey=path.qkey);
        self._qp.post_send(wr);

    def _cq_sleep(self,wakeat):
        """Go to sleep until we get a completion."""
        while True:
            if wakeat is None:
                ret = self._poll.poll(0);
            else:
                timeout = wakeat - rdma.tools.clock_monotonic();
                if timeout <= 0:
                    return None;
                ret = self._poll.poll(timeout*1000);
            if ret is None:
                return None;
            for I in ret:
                if self._cc.check_poll(I) is not None:
                    return True;

    def _cq_drain(self):
        """Empty the CQ and return and send buffers back to the pool. receive
        buffers are queued onto :attr:`_recvs` for later retrieval."""
        wcs = self._cq.poll();
        if not wcs:
            return;
        for I,wc in enumerate(wcs):
            if wc.opcode == ibv.IBV_WC_RECV and wc.status == ibv.IBV_WC_SUCCESS:
                wcs[I] = None;
                self._recvs.appendleft(wc);
        self._pool.finish_wcs(self._qp,wcs);

    def recvfrom(self,wakeat):
        '''Receive a MAD packet. If the value of
        :func:`rdma.tools.clock_monotonic()` exceeds *wakeat* then :class:`None`
        is returned.

        :returns: tuple(buf,path)'''
        while True:
            if self._recvs:
                wc = self._recvs.pop();
                buf = self._pool.copy_from(wc.wr_id,40,wc.byte_len);
                self._pool.finish_wcs(self._qp,(wc,));
                return (buf,ibv.WCPath(self.end_port,wc,
                                       self._pool._mem,
                                       wc.wr_id*self._pool.size,
                                       pkey=self.pkey,
                                       qkey=self.qkey));

            self._cq_drain();
            if not self._recvs:
                if not self._cq_sleep(wakeat):
                    return None;

    def _execute(self,buf,path,sendOnly = False):
        """Send the fully formed MAD in buf to path and copy the reply
        into buf. Return path of the reply. This is a synchronous method, all
        MADs received during this call are discarded until the reply is seen."""
        self.sendto(buf,path);
        if sendOnly:
            return None;
        rmatch = self._get_reply_match_key(buf);
        expire = path.mad_timeout + rdma.tools.clock_monotonic();
        retries = path.retries;
        while True:
            ret = self.recvfrom(expire);
            if ret is None:
                if retries == 0:
                    return None;
                retries = retries - 1;
                self._execute(buf,path,True);

                expire = path.mad_timeout + rdma.tools.clock_monotonic();
                continue;
            elif rmatch == self._get_match_key(ret[0]):
                return ret;
            else:
                if self.trace_func is not None:
                    self.trace_func(self,rdma.madtransactor.TRACE_UNEXPECTED,
                                    path=path,ret=ret);

    def __enter__(self):
        return self;

    def __exit__(self,*exc_info):
        return self.close();

    def close(self):
        """Free the resources held by the object."""
        if self._pool is not None:
            self._pool.close();
        if self._pd is not None:
            self._pd.close();
        if self._cq is not None:
            self._cq.close();
        if self._cc is not None:
            self._cc.close();
        if self._allocated_ctx and self._ctx is not None:
            self._ctx.close();

    def _get_new_TID(self):
        self._tid = (self._tid + 1) % (1 << 32);
        return self._tid;

    def __repr__(self):
        return "<%s.%s object for %s at 0x%x>"%\
               (self.__class__.__module__,
                self.__class__.__name__,
                self.end_port,
                id(self));

# Copyright 2011 Obsidian Research Corp. GPLv2, see COPYING.
import rdma;
import logging;
import collections;
import rdma.path;
import rdma.satransactor;
import rdma.IBA as IBA;

def subnet_ninf_GUID(sched,sbn,node_guid):
    """Coroutine to fetch a :class:`~rmda.IBA.SMPNodeInfo` record from the
    SA for a specific GUID and store it in *sbn*."""
    req = IBA.ComponentMask(IBA.SANodeRecord());
    req.nodeInfo.nodeGUID = node_guid;
    res = yield sched.SubnAdmGetTable(req);

    # The SM can return multiple records that match a nodeGUID, one for each port
    # on a CA. When it does this it must set the portGUID and localPortNum correctly
    # to match the LID in the RID.
    for I in res:
        np = sbn.get_node_ninf(I.nodeInfo,LID=I.LID);
        np[0].set_desc(I.nodeDescription.nodeString);

def subnet_ninf_SA(sched,sbn,node_type=None):
    """Coroutine to fetch all :class:`~rmda.IBA.SMPNodeInfo` records from the
    SA and store them in *sbn*."""
    req = IBA.ComponentMask(IBA.SANodeRecord());
    if node_type is not None:
        req.nodeInfo.nodeType = node_type;
    res = yield sched.SubnAdmGetTable(IBA.SANodeRecord());
    if res:
        sbn.set_max_lid(max(I.LID for I in res));
    for I in res:
        np = sbn.get_node_ninf(I.nodeInfo,LID=I.LID);
        np[0].set_desc(I.nodeDescription.nodeString);
    if node_type is None:
        sbn.loaded.add("all_NodeInfo");
        sbn.loaded.add("all_NodeDescription");
    else:
        sbn.loaded.add("all_NodeInfo %u"%(node_type));
        sbn.loaded.add("all_NodeDescription %u"%(node_type));

def subnet_swinf_SA(sched,sbn):
    """Coroutine to fetch all :class:`~rdma.IBA.SMPSwitchInfo` records
    from the SA and store them in *sbn*."""
    res = yield sched.SubnAdmGetTable(IBA.SASwitchInfoRecord());
    if res:
        sbn.set_max_lid(max(I.LID for I in res));
    for I in res:
        np = sbn.get_node(type_=rdma.subnet.Switch,portIdx=0,LID=I.LID);
        np[0].swinf = I.switchInfo;
    sbn.loaded.add("all_SwitchInfo");

def _subnet_fill_LIDs_SA(sched,sbn,LMC):
    """Coroutine to ask the SA for all the PortInfo's with LMC=LMC."""
    pinf = IBA.ComponentMask(IBA.SAPortInfoRecord())
    pinf.portInfo.LMC = LMC;
    res = yield sched.SubnAdmGetTable(pinf);
    if res:
        sbn.set_max_lid(max(I.endportLID for I in res));
    for I in res:
        assert I.endportLID == I.portInfo.LID;
        sbn.get_port_pinf(pinf,portIdx=I.portNum);

def subnet_fill_LIDs_SA(sched,sbn):
    """Coroutine to fill in the LID mapping in *sbn* to compensate for LMC.
    This should be called after :func:`subnet_ninf_SA` if desired."""
    assert "all_NodeInfo" in sbn.loaded;

    # This returns the portinfo records with a non-zero LMC. IMHO it is
    # foolish for IBA to not return LMC as well in NodeRecord..
    # FIXME returns all switch ports too :( :(
    yield sched.mqueue(_subnet_fill_LIDs_SA(sched,sbn,LMC) for LMC in range(1,8));

    sbn.loaded.add("all_LIDs");

def subnet_topology_SA(sched,sbn):
    """Coroutine to fill in the topology in *sbn*."""
    assert "all_NodeInfo" in sbn.loaded;
    res = yield sched.SubnAdmGetTable(IBA.SALinkRecord);

    sbn.topology = {};
    for I in res:
        # The fromPort/toPort is reserved if the node is not a switch,
        # don't use it.
        fn,f = sbn.get_node(rdma.subnet.Node,LID=I.fromLID);
        tn,t = sbn.get_node(rdma.subnet.Node,LID=I.toLID);
        if isinstance(fn,rdma.subnet.Switch):
            f = sbn.get_port(portIdx=I.fromPort,LID=I.fromLID);
        if isinstance(tn,rdma.subnet.Switch):
            t = sbn.get_port(portIdx=I.toPort,LID=I.toLID);

        sbn.topology[f] = t;
        sbn.topology[t] = f;
    sbn.loaded.add("all_topology");

def subnet_pinf_SA(sched,sbn):
    """Coroutine to ask the SA for all :class:`~rdma.IBA.SMPPortInfo`."""
    assert "all_NodeInfo" in sbn.loaded;

    res = yield sched.SubnAdmGetTable(IBA.SAPortInfoRecord);
    for I in res:
        sbn.get_port_pinf(I.portInfo,portIdx=I.portNum,LID=I.endportLID);
    sbn.loaded.add("all_PortInfo");
    sbn.loaded.add("all_LIDs");

def subnet_pinf_SMP(sched,sbn,sel,path):
    """Coroutine to send SMPs to get :class:`~rdma.IBA.SMPPortInfo`."""
    pinf = yield sched.SubnGet(IBA.SMPPortInfo,path,sel);
    sbn.get_port_pinf(pinf,port_select=sel,path=path);

def subnet_ninf_SMP(sched,sbn,path,get_desc=True,use_sa=None,done_desc=None):
    """Coroutine to send SMPs to get :class:`~rdma.IBA.SMPNodeInfo` and
    :class:`~rdma.IBA.SMPNodeDescription`. The result of the co-routine
    is the same as :meth:`rdma.subnet.Subnet.get_node_ninf`. If *sched*
    is a :class:`rdma.satransactor.SATransactor` then the query is optimized
    into a combined node info/node description SA query."""
    if use_sa is None:
        use_sa = isinstance(sched,rdma.satransactor.SATransactor);

    if use_sa:
        req = IBA.ComponentMask(IBA.SANodeRecord());
        req.LID = yield sched.prepare_path_lid(path);
        ret = yield sched.SubnAdmGet(req);
        sched.result = sbn.get_node_ninf(ret.nodeInfo,path);
        sched.result[0].set_desc(ret.nodeDescription.nodeString);
        path._cached_node_type = ret.nodeInfo.nodeType;
    else:
        ninf = yield sched.SubnGet(IBA.SMPNodeInfo,path);
        result = sbn.get_node_ninf(ninf,path);
        sched.result = result;
        node,port = result

        if node.desc is not None or get_desc is False:
            return;
        if done_desc is not None:
            if node in done_desc:
                return;
            done_desc.add(node);
        sched.queue(node.get_desc(sched,path));
        sched.result = result;

def subnet_ninf_LIDS_SMP(sched,sbn,LIDs,get_desc=True):
    """Generator to fetch :class:`~rdma.IBA.SMPNodeInfo` and
    :class:`~rdma.IBA.SMPNodeDescription` for every LID in *LIDs* and store
    them in *sbn*. This does not refetch existing info."""
    LIDs.sort();
    max_lid = LIDs[-1];
    sbn.set_max_lid(max_lid);

    # Spread out the LIDs we search since we don't know the LMC..  This
    # decreases the chance we will probe the same port twice.
    # FIXME: This made more sense when this got the pinf too....
    def rotate(x):
        x = (x << 16) | x;
        return (x >> 7) & 0xFFFF;
    LIDs.sort(key=rotate);

    def get_path(lid):
        return rdma.path.IBPath(sched.end_port,SLID=sched.end_port.lid,DLID=lid,
                                dqpn=0,sqpn=0,qkey=IBA.IB_DEFAULT_QP0_QKEY);

    use_sa = isinstance(sched,rdma.satransactor.SATransactor);

    done = set();
    if get_desc:
        done_desc = set();
    else:
        done_desc = None;
    for I in LIDs:
        node = None;
        port = sbn.lids[I];
        if port is not None:
            node = port.parent;
        if node is not None:
            if get_desc and node.desc is None:
                done_desc.add(node);
                yield node.get_desc(sched,get_path(I));
            if node.ninf is not None or node in done:
                continue;
            done.add(node);
        yield subnet_ninf_SMP(sched,sbn,get_path(I),get_desc,use_sa,done_desc);

def subnet_swinf_SMP(sched,sbn):
    """Coroutine to fetch all :class:`rdma.IBA.SMPSwitchInfo`."""
    assert("all_NodeInfo" in sbn.loaded or
           "all_NodeInfo %u"%(IBA.NODE_SWITCH) in sbn.loaded);

    yield sched.mqueue(I.get_switch_inf(sched,sbn.get_path_smp(sched,I.ports[0]))
                       for I in sbn.iterswitches() if I.swinf is None);

    sbn.loaded.add("all_SwitchInfo");

class _SubnetTopo(object):
    """This scans the topology of of a subnet using SMPs either with pure
    directed route or a combination of directed route a LID routing. This is
    more complex than the dumb basic approach because it enforces breadth
    first ordering of the DR paths. This produces shortest paths to all nodes
    and thus gives us the best chance of not exceeding the hop count on large
    networks."""
    class _Depth(object):
        ctx = None;
        todo_nodes = None;
        todo_ports = None;
        running = False;
        def __init__(self):
            self.todo_nodes = [];
            self.todo_ports = [];

    # FIXME: Error handling strategy :(
    done_desc = None;
    lid_route = True;

    def __init__(self,sched,sbn,get_desc,lid_route):
        self.sched = sched;
        self.sbn = sbn;
        self.lid_route = lid_route;
        self.todo = collections.defaultdict(_SubnetTopo._Depth);
        self.done_switches = set();
        if get_desc:
            self.done_desc = set();

    def sched_node(self,aport,path,portIdx,depth):
        """Queue reading the node attached to *aport*."""
        bucket = self.todo[depth];
        bucket.todo_nodes.append((aport,path,portIdx));
        if not bucket.running:
            bucket.running = True;
            bucket.ctx = self.sched.mqueue(self.do_todo(bucket.ctx,bucket,
                                                        depth));

    def sched_ports(self,node,path,portIdx,depth):
        """Queue reading the PortInfos attached to node."""
        bucket = self.todo[depth];
        bucket.todo_ports.append((node,path,portIdx,depth));
        if not bucket.running:
            bucket.running = True;
            bucket.ctx = self.sched.mqueue(self.do_todo(bucket.ctx,bucket,
                                                        depth));

    def do_todo(self,old_ctx,bucket,depth):
        """Generator to get the node and port infos. This is centralized
        to enforce the BFS order, each *depth* gets a single operating
        version of this generator."""
        try:
            # We can fetch the topology for our current depth whenever
            # We have finished fetching topology for depth-2. This way we
            # know we cannot get a shorter path to the current depth.
            pbucket = self.todo[depth-2];
            while pbucket.ctx:
                tmp = pbucket.ctx;
                yield tmp;
                if pbucket.ctx == tmp:
                    pbucket.ctx = None;
            last = None;
            while bucket.todo_nodes or bucket.todo_ports:
                if bucket.todo_nodes:
                    aport,path,portIdx = bucket.todo_nodes.pop();
                    peer = self.sbn.topology.get(aport);
                    if (peer is not None and peer.parent is not None and
                        peer.parent.ninf is not None):
                        continue;

                    # Defer computing the next hop path for as long as
                    # possible to give the best chance of becoming a LID
                    # routed path.
                    npath = self.sbn.advance_dr(path,portIdx);
                    last = yield self.do_node(npath,depth,aport);

                if bucket.todo_ports:
                    node,path,portIdx,depth = bucket.todo_ports.pop();
                    r = ((portIdx,) if portIdx is not None else
                         range(0,node.ninf.numPorts+1));
                    for portIdx in r:
                        aport = node.get_port(portIdx);
                        if aport.pinf is not None:
                            continue;
                        yield self.do_port(path,node,aport,portIdx,depth);

                    # Get the description too, we do this after
                    # the ports so it has a better chance of being LID
                    # routed.
                    if self.done_desc is not None:
                        if node.desc is not None or node in self.done_desc:
                            continue;
                        self.done_desc.add(node);
                        yield node.get_desc(self.sched,path);
        finally:
            bucket.running = False;

        # Wait for all the work started by the CTX we are replacing to
        # finish.
        yield old_ctx;

    def do_port(self,path,node,aport,portIdx,depth):
        """Coroutine to get a :class:`~rdma.IBA.SMPPortInfo` and schedule
        scanning the attached node, if applicable."""
        try:
            pinf = yield self.sched.SubnGet(IBA.SMPPortInfo,path,portIdx)
        except rdma.RDMAError as e:
            logging.error(e)
            pinf = IBA.SMPPortInfo()
            pinf.portState = IBA.PORT_STATE_DOWN
        aport = self.sbn.get_port_pinf(pinf,path=path,portIdx=portIdx);

        if self.lid_route and isinstance(path,rdma.path.IBDRPath):
            # The first pinf we get for the path with a LID transforms it into
            # a LID path. Some switches will return the LID on all ports,
            # others return 0 for everything but port 0.
            if (pinf.LID != 0 and pinf.LID < IBA.LID_MULTICAST):
                path.SLID = path.end_port.lid;
                path.DLID = pinf.LID;
                path.__class__ = rdma.path.IBPath;
                tmp = path._cached_subnet_end_port;
                path.drop_cache();
                path._cached_subnet_end_port = tmp;
                delattr(path,"drPath");

        if (pinf.portState == IBA.PORT_STATE_DOWN or
            portIdx == 0 or
            aport in self.sbn.topology):
            return;

        self.sched_node(aport,path,portIdx,depth+1);

    def do_node(self,path,depth=0,peer=None):
        """Coroutine to get the :class:`~rdma.IBA.SMPNodeInfo` and scan all the
        port infos."""
        try:
            ninf = yield self.sched.SubnGet(IBA.SMPNodeInfo,path);
        except rdma.RDMAError as err:
            logging.error(err) 
            return

        node,port = self.sbn.get_node_ninf(ninf,path);

        if isinstance(node,rdma.subnet.Switch):
            if peer is not None:
                aport = node.get_port(ninf.localPortNum);
                self.sbn.topology[aport] = peer;
                self.sbn.topology[peer] = aport;

            # This check is just an optimization, the check for pinf == None
            # does the same.. Don't need to do it on HCA ports since there
            # is only one peer port for them and that will be protected by
            # the topology update.
            if node not in self.done_switches:
                self.done_switches.add(node);
                self.sched_ports(node,path,None,depth);
        else:
            if peer is not None:
                self.sbn.topology[port] = peer;
                self.sbn.topology[peer] = port;
            self.sched_ports(node,path,ninf.localPortNum,depth);

def topo_SMP(sched,sbn,get_desc=True):
    """Generator to fetch an entire subnet topology using SMPs."""
    # Wipe out existing volatile information. Maybe nodeDescription too?
    sbn.topology = {};
    for I,idx in sbn.iterports():
        I.pinf = None;

    fetcher = _SubnetTopo(sched,sbn,get_desc,sbn.lid_routed);
    if sbn.lid_routed:
        path = rdma.path.IBPath(sched.end_port,SLID=sched.end_port.lid,
                                DLID=sched.end_port.lid,
                                dqpn=0,sqpn=0,qkey=IBA.IB_DEFAULT_QP0_QKEY);
    else:
        sbn.paths = {};
        path = rdma.path.IBDRPath(sched.end_port);
    yield fetcher.do_node(path);

    sbn.loaded.add("all_LIDs");
    sbn.loaded.add("all_NodeInfo");
    if get_desc:
        sbn.loaded.add("all_NodeDescription");
    sbn.loaded.add("all_topology");

def topo_peer_SMP(sched,sbn,port,get_desc=True,path=None,
                  peer_path=None):
    """Coroutine to fetch a single connected peer. This updates
    :attr:`rdma.subnet.Subnet.topology`. It also fetches a port info to setup
    LID routing.

    *peer_path* is the path out *port*, created by
     :meth:`rdma.subnet.Subnet.advance_dr`.

    This does nothing if the information is already loaded."""
    peer_port = sbn.topology.get(port);
    if peer_port is None:
        portIdx = port.port_id;

        if peer_path is None:
            if path is None:
                path = sbn.get_path_smp(sched,port.to_end_port());
            peer_path = sbn.advance_dr(path,portIdx);

        if port.pinf is None:
            yield subnet_pinf_SMP(sched,sbn,portIdx,path);

        if (port.pinf.portState == IBA.PORT_STATE_DOWN or
            portIdx == 0):
            return

        # Resolve the DR path using the SA and update our topology information
        # as well.
        use_sa = isinstance(sched,rdma.satransactor.SATransactor);
        if use_sa:
            if path is None:
                path = sbn.get_path_smp(sched,port.to_end_port());
            req = IBA.ComponentMask(IBA.SALinkRecord());
            req.fromLID = yield sched.prepare_path_lid(path);
            req.fromPort = portIdx;
            rep = yield sched.SubnAdmGet(req);
            peer_path._cached_resolved_dlid = rep.toLID;
            peer_port = sbn.get_port(portIdx=rep.toPort,LID=rep.toLID,
                                     path=peer_path);

        peer_node,peer_zport = yield subnet_ninf_SMP(sched,sbn,peer_path,
                                                     get_desc,use_sa);
        get_desc = False;
        if not use_sa:
            lpn = getattr(peer_path,"_cached_subnet_localPortNum",
                          peer_node.ninf.localPortNum);
            peer_port = sbn.get_port(portIdx=lpn,
                                     path=peer_path);

        sbn.topology[port] = peer_port;
        sbn.topology[peer_port] = port;
    else:
        peer_node = peer_port.parent;
        peer_zport = peer_port.to_end_port();
        peer_path = sbn.get_path_smp(sched,peer_zport);

    if get_desc and peer_node.desc is None:
        sched.queue(peer_node.get_desc(sched,peer_path));

    if sbn.lid_routed and peer_zport.LID is None:
        yield subnet_pinf_SMP(sched,sbn,0,peer_path);

def topo_surround_SMP(sched,sbn,node,get_desc=True):
    """Coroutine to fetch everything connected to all end ports on *node*.  This
    updates :attr:`rdma.subnet.Subnet.topology`. It also fetches a port info
    to setup LID routing.

    This does nothing if the information is already loaded."""
    for I in node.iterend_ports():
        path = sbn.get_path_smp(sched,I);

        if node.ninf is None:
            yield subnet_ninf_SMP(sched,sbn,path,get_desc);
        else:
            if node.desc is None:
                sched.queue(node.get_desc(sched,path));
        ninf = node.ninf;

        def do_port(sel,path):
            aport = node.get_port(sel);
            if aport.pinf is None:
                pinf = yield sched.SubnGet(IBA.SMPPortInfo,path,sel);
                sbn.get_port_pinf(pinf,port_select=sel,path=path);
            else:
                pinf = aport.pinf;
            if pinf.portState != IBA.PORT_STATE_DOWN:
                yield topo_peer_SMP(sched,sbn,aport,get_desc);

        if isinstance(node,rdma.subnet.Switch):
            zport = node.get_port(0);
            if zport.pinf is None:
                sched.queue(subnet_pinf_SMP(sched,sbn,0,path));
            sched.mqueue(do_port(sel,path) for sel in range(1,ninf.numPorts+1));
        else:
            sched.queue(do_port(node.ports.index(I),path));

def subnet_fill_port(sched,sbn,port,path=None,get_desc=True):
    """Generator to fill in the `pinf`, `ninf`, and `desc` for
    *port*. This will also collect the end port `pinf` as well as the
    `pinf` for *port* if they are different.

    This does nothing if the information is already loaded."""
    node = port.parent;
    port_ep = port.to_end_port();
    if path is None:
        path = sbn.get_path_smp(sched,port_ep);
    if node.ninf is None:
        yield rdma.discovery.subnet_ninf_SMP(sched,sbn,path,get_desc);
        get_desc = False;
    if get_desc and node.desc is None:
        yield node.get_desc(sched,path);
    if port.pinf is None:
        yield rdma.discovery.subnet_pinf_SMP(sched,sbn,
                                             node.ports.index(port),
                                             path);
    if port_ep.pinf is None and port_ep != port:
        yield rdma.discovery.subnet_pinf_SMP(sched,sbn,
                                             node.ports.index(port_ep),
                                             path);

def subnet_get_port(sched,sbn,path,get_desc=True):
    """Coroutine to associate *path* with a :class:`rdma.subnet.Port`
    structure with a filled in `pinf`, `ninf` and `desc`. The
    :class:`~rdma.subnet.Port` can be retrieved through
    :meth:`rdma.subnet.Subnet.path_to_port`.

    This does nothing if the information is already loaded."""
    ret = sbn.path_to_port(path);
    if ret is None:
        yield subnet_ninf_SMP(sched,sbn,path,get_desc);
        ret = sbn.path_to_port(path);
        get_desc = False;
    yield sched.mqueue(subnet_fill_port(sched,sbn,ret,path,get_desc));

def load(sched,sbn,stuff):
    """Fill *sbn* with the discovery items in *stuff*. *stuff* may be a list
       of strings where each string is one of:

       * `all_LIDs` fill in *sbn.lids* completely.
       * `all_NodeInfo` may be followed by a space and then a node type number.
       * `all_PortInfo`
       * `all_NodeDescription` may be followed by a space and then a node type number.
       * `all_SwitchInfo`
       * `all_topology`
       """
    if not isinstance(stuff,set):
        stuff = set(stuff);

    if "all_NodeDescription" in stuff:
        stuff.add("all_NodeInfo");
    if "all_PortInfo" in stuff:
        stuff.add("all_NodeInfo");
    if "all_LIDs" in stuff:
        stuff.add("all_NodeInfo");

    # Deal with a qualifying space after the thing eg 'all_NodeInfo 1'
    non_sa = set();
    for I in stuff:
        sp = I.split(' ')
        if len(sp) > 1:
            if sp[0] in sbn.loaded:
                sbn.loaded.add(I);
            else:
                non_sa.add(sp[0]);

    stuff.difference_update(sbn.loaded);

    def fetch_SA(sched):
        if "all_SwitchInfo" in stuff:
            yield subnet_swinf_SA(sched,sbn);
            stuff.add("all_NodeInfo %u"%(IBA.NODE_SWITCH));

        if "all_NodeInfo" in stuff or "all_NodeDescription" in stuff:
            yield subnet_ninf_SA(sched,sbn);
        else:
            doing = set();
            for I in stuff:
                sp = I.split(' ');
                if len(sp) > 1 and (sp[0] == "all_NodeInfo" or sp[0] == "all_NodeDescription"):
                    ty = int(sp[1])
                    if ty in doing:
                        continue;
                    doing.add(ty);
                    yield subnet_ninf_SA(sched,sbn,node_type=ty);

    def fetch_SA2(sched):
        if "all_PortInfo" in stuff:
            yield subnet_pinf_SA(sched,sbn);
        else:
            if "all_LIDs" in stuff:
                yield subnet_fill_LIDs_SA(sched,sbn);
        if "all_topology" in stuff:
            yield subnet_topology_SA(sched,sbn);

    if isinstance(sched,rdma.satransactor.SATransactor):
        sched.run(mqueue=fetch_SA(sched));
        sched.run(mqueue=fetch_SA2(sched));
    else:
        if "all_SwitchInfo" in stuff:
            stuff.add("all_NodeInfo");

        stuff.update(non_sa);
        stuff.difference_update(sbn.loaded);

        if ("all_LIDs" in stuff or "all_NodeInfo" in stuff or
            "all_NodeDescription" in stuff or "all_topology" in stuff):
            sched.run(queue=topo_SMP(sched,sbn,
                                     "all_NodeDescription" in stuff));
        if "all_SwitchInfo" in stuff:
            sched.run(queue=subnet_swinf_SMP(sched,sbn));

    stuff.difference_update(sbn.loaded);

    if "all_PortInfo" in stuff:
        sched.run(mqueue=(subnet_pinf_SMP(sched,sbn,I[1],
                                          sbn.get_path_smp(sched,I[0].to_end_port()))
                          for I in sbn.iterports() if I[0].pinf is None));
        sbn.loaded.add("all_PortInfo");
        sbn.loaded.add("all_LIDs");

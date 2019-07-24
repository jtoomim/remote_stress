from jsonrpcclient import request
import time, random, string, traceback, threading, json, http
from decimal import Decimal

class RemoteTestRunner:
    def __init__(self, host, port, testname=None, NAT=False):
        self.host = host
        self.port = port
        self.url = 'http://' + host + ":" + str(port)
        self.testname = testname
        self.NAT = NAT
        self.nodes = []

    def init_test(self, num_nodes, *args, **kwargs):
        if self.testname == None:
            self.testname = ''.join([random.choice(string.ascii_letters + string.digits) for i in range(10)])
            print("Test name is %s" % self.testname)
        self.wan_ip = ""# self.get_wan_ip()
        #print(str(self) + "'s WAN IP is %s" % self.wan_ip)
        result = self.add_test(num_nodes, *args, **kwargs)
        if result == True:
            ports = self.get_node_p2p_ports()
            self.nodes = [RemoteNode(self, i, self.host, ports[i]) for i in range(num_nodes)]

    def __getattr__(self, name):
        def rpc_call(*args, **kwargs):
            response = request(self.url, name, self.testname, *args, **kwargs)
            return response.data.result
        return rpc_call
    def __str__(self):
        return "RemoteTestRunner(%16s, %5i, %s)" % (self.host, self.port, self.testname)


class RemoteNode:
    def __init__(self, testrunner, ID, host, p2p_port):
        self.ID = ID
        self.testrunner = testrunner
        self.host = host
        self.p2p_port = p2p_port

    def __getattr__(self, name):
        def rpc_call(*args, **kwargs):
            return self.testrunner.send_node_command(self.ID, name, *args, **kwargs)
        return rpc_call


def connect_round_robin(machinelist, chain=False):
    maxnodecount = max([len(machine.nodes) for machine in machinelist])
    nodescores = []
    for m, mach in zip(range(len(machinelist)), machinelist):
        msize = len(mach.nodes)
        # m/9999 is 
        nodescores.extend([(m/(msize*100+1.) + i/float(msize), (m, i)) for i in range(msize)])
    nodescores.sort()
    for n in range(len(nodescores)):
        if not chain and n == len(nodescores)-1: 
            break # not making a loop, so don't connect the last to the first
        m, i = nodescores[n][1]
        node = machinelist[m].nodes[i]
        nxt = n+1 if n+1 < len(nodescores) else 0
        j, k = nodescores[nxt][1]
        target = machinelist[j].nodes[k]
        src_ip_port = node.testrunner.host + ":" + str(node.p2p_port)
        dst_ip_port = target.testrunner.host + ":" + str(target.p2p_port)
        src_wan_ip_port = node.testrunner.wan_ip + ":" + str(node.p2p_port)
        dst_wan_ip_port = target.testrunner.wan_ip + ":" + str(target.p2p_port)
        print("Connecting %i,%i to %i,%i (port %s to %s)" % (nodescores[n][1] + nodescores[nxt][1] + (src_wan_ip_port, dst_wan_ip_port)))
        node.addnode(dst_ip_port, "onetry")
        target.addnode(src_ip_port, "onetry")
        #if not src_wan_ip_port == src_ip_port:
        # if not target.testrunner.NAT:
        #     node.addnode(dst_wan_ip_port, "onetry")
        # #if not dst_wan_ip_port == dst_ip_port:
        # if not node.testrunner.NAT:
        #     target.addnode(src_wan_ip_port, "onetry")

        # poll until version handshake complete to avoid race conditions
        # with transaction relaying
        # this check has been disabled because it will likely be a big slowdown
        # but maybe it can be done fast enough in a separate loop at the end
        while any(peer['version'] == 0 for peer in node.getpeerinfo()):
            time.sleep(0.1)

def do_to_machines(machines, command, *args, **kwargs):
    results = []
    def helper(machine):
        results.append(getattr(machine, command)(*args, **kwargs))
    threads = [threading.Thread(target=helper, args=(machine,)) for machine in machines]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    return results
def do_to_nodes(machines, command, *args, **kwargs):
    raise NotImplemented # finish me before use
    results = []
    def helper(machine):
        results.append(getattr(machine, command)(*args, **kwargs))
    threads = [threading.Thread(target=helper, args=(machine,)) for machine in machines]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    return results

def sync(machines, wait=0.1, timeout=5.):
    stop_time = time.time() + timeout
    while time.time() <= stop_time:
        best_hash = [node.getbestblockhash() for machine in machines for node in machine.nodes]
        if best_hash.count(best_hash[0]) == len(best_hash):
            return
        time.sleep(wait)
    for machine in machines:
        print("Machine %50s heights:" % str(machine), [node.getblock(node.getbestblockhash())['height'] for node in machine.nodes])
    raise AssertionError("Block sync timed out:{}".format(
        "".join("\n  {!r}".format(b) for b in best_hash)))


def make_utxos(gen, machines, target):
    print("Running make_utxos")
    fanout = 500
    flatnodes = []
    addresses = do_to_machines(machines, 'get_many_addresses', fanout)
    for i in range(len(machines)):
        flatnodes.extend(machines[i].nodes)
        for node, adds in zip(machines[i].nodes, addresses[i]):
            node.addresses = adds
    print(" - Addresses generated")
    rootamount = 1.
    num_stages = -(-target // fanout) +1 # rounds up
    print(" - Fanout=%i, num_stages=%i" % (fanout, num_stages))
    gen.generate(101)
    for machine in machines:
        print("Machine %50s heights:" % str(machine), [node.getblock(node.getbestblockhash())['height'] for node in machine.nodes])
    time.sleep(0.2)
    gen.generate(1)
    sync(machines, timeout=10.)
    amount = str(Decimal(round(rootamount/(fanout+1) * 1e8)) / Decimal(1e8))
    for node in flatnodes:
        if node == gen: # don't pollute wallet
            continue
        payments = {node.addresses[n]:amount for n in range(fanout)}
        t1 = time.time()
        for stage in range(num_stages):
            gen.generate(1)
            gen.sendmany('', payments)
        print("Balance is", gen.getbalance())
        t2 = time.time(); print("Filling node wallets took %3.3f sec for stage %i:%i" % (t2-t1, flatnodes.index(node), stage))
    for i in range(1+(target*len(flatnodes)//20000)):
        gen.generate(1)
        sync(machines, timeout=10)
        blk = gen.getblock(gen.getbestblockhash(), 1)
        print("Block has %i transactions and is %i bytes" % (len(blk['tx']), blk['size']))
    return amount

def generate_spam(gen, machines, value, txcount, rate=1000):
    spamnodes = [node for machine in machines for node in machine.nodes if not node == gen]
    def helper(node, count):
        batchsize = 100
        t = time.time()
        for i in range(0, count, batchsize):
            now = time.time()
            if i/(now-t) > rate:
                time.sleep(i/rate - (now-t))
            if not (i%1000):
                print("Node %2i\ttx %5i\tat %3.3f sec\t(%3.0f tx/sec)" % (spamnodes.index(node)+1, i, time.time()-t, (i/(time.time()-t))))
            add = node.addresses[i % len(node.addresses)]
            try:
                node.sendtoaddress(add, value, '', '', False, batchsize)
            except http.client.CannotSendRequest: # hack to bypass lack of thread safety in http.client
                node.sendtoaddress(add, value, '', '', False, batchsize)
            except:
                print("Node %i had a fatal error on tx %i:" % (spamnodes.index(node), i))
                traceback.print_exc()
                break
    threads = [threading.Thread(target=helper, args=(node, txcount)) for node in spamnodes]

    t0 = time.time()
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    t1 = time.time(); print("Generating spam took %3.3f sec for %i tx (total %4.0f tx/sec)" \
        % (t1-t0, (len(spamnodes))*txcount, (len(spamnodes))*txcount/(t1-t0)))



testname = ''.join([random.choice(string.ascii_letters + string.digits) for i in range(10)])
print("Test name is %s" % testname)

# machines = [#RemoteTestRunner("10.140.1.246", 10999, testname, NAT=True),
#             RemoteTestRunner("208.84.223.121", 6000, testname),
#             RemoteTestRunner("208.84.223.121", 6100, testname)]

machines = [#RemoteTestRunner("10.140.1.246", 10999, testname, NAT=True),
            RemoteTestRunner("10.0.1.7", 6000, testname),
            RemoteTestRunner("10.0.1.8", 6100, testname)]


try:
    for machine in machines:
        machine.info = machine.getcpuinfo()
        if 'cores' in machine.info:
            num_nodes = machine.info['cores']
            print("%s has %i cores" %(str(machine), machine.info['cores']))
        else:
            num_nodes = 4
            print("Remote host didn't tell us how many cores it has. Assuming %i." % num_nodes)
        machine.init_test(num_nodes)

    connect_round_robin(machines, chain=True)
    for machine in machines:
        print("%50s connections:" % machine, [node.getconnectioncount() for node in machine.nodes])
    #print(json.dumps(machines[0].nodes[0].getpeerinfo()[0], indent=4, sort_keys=True))
    nodecount = sum([len(machine.nodes) for machine in machines])
    txcount = 16800 // (nodecount-1)
    spend_value = amount = make_utxos(machines[0].nodes[0], machines, int(txcount*1.2))

    for i in range(5):
        spend_value = str(Decimal((Decimal(spend_value) * 100000000 - 192)) / Decimal(1e8))
        print(amount, spend_value)
        generate_spam(machines[0].nodes[0], machines, spend_value, txcount, 1000)

        t0 = time.time()
        machines[0].nodes[0].generate(1)
        sync(machines, .1, 120.)
        t1 = time.time()
        print("Propagating block took %3.3f sec" % (t1-t0))
        blk = machines[0].nodes[0].getblock(machines[0].nodes[0].getbestblockhash(), 1)
        print("Block has %i transactions and is %i bytes" % (len(blk['tx']), blk['size']))

except:
    traceback.print_exc()
    nothing = input()
finally:
    for machine in machines:
        try:
            print("Machine %50s.end_test(): " % str(machine), machine.end_test())
        except:
            traceback.print_exc()

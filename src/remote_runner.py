from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal, sync_blocks, wait_until, check_json_precision, PortSeed
import test_framework.util
from decimal import Decimal
from enum import Enum
import argparse
import configparser
import logging
import tempfile
import shutil
import random
import subprocess
import jsonrpcserver
from jsonrpcserver import method, serve, dispatch
from http.server import BaseHTTPRequestHandler, HTTPServer
import traceback, sys, os, requests
import time, threading, traceback, http, sys

class TestStatus(Enum):
    PASSED = 1
    FAILED = 2
    SKIPPED = 3
TEST_EXIT_PASSED = 0
TEST_EXIT_FAILED = 1
TEST_EXIT_SKIPPED = 77
# Timestamp is 01.01.2019
TIMESTAMP_IN_THE_PAST = 1546300800


def parse_config_and_options():
    parser = argparse.ArgumentParser(usage="%(prog)s [options]")
    parser.add_argument("--nocleanup", dest="nocleanup", default=False, action="store_true",
                        help="Leave bitcoinds and test.* datadir on exit or error")
    parser.add_argument("--noshutdown", dest="noshutdown", default=False, action="store_true",
                        help="Don't stop bitcoinds after the test execution")
    parser.add_argument("--srcdir", dest="srcdir", default=os.path.abspath(os.path.dirname(os.path.realpath(__file__)) + "/../../../src"),
                        help="Source directory containing bitcoind/bitcoin-cli (default: %(default)s)")
    parser.add_argument("--cachedir", dest="cachedir", default=os.path.abspath(os.path.dirname(os.path.realpath(__file__)) + "/../../cache"),
                        help="Directory for caching pregenerated datadirs (default: %(default)s)")
    parser.add_argument("--tmpdir", dest="tmpdir",
                        help="Root directory for datadirs")
    parser.add_argument("--walletdir", dest="walletdir",
                        help="Root directory for walletdirs")
    parser.add_argument("-l", "--loglevel", dest="loglevel", default="INFO",
                        help="log events at this level and higher to the console. Can be set to DEBUG, INFO, WARNING, ERROR or CRITICAL. Passing --loglevel DEBUG will output all logs to console. Note that logs at all levels are always written to the test_framework.log file in the temporary test directory.")
    parser.add_argument("--tracerpc", dest="trace_rpc", default=False, action="store_true",
                        help="Print out all RPC calls as they are made")
    parser.add_argument("--portseed", dest="port_seed", default=random.randint(1, 10000), type=int,
                        help="The seed to use for assigning port numbers (default: random integer in 0..10000)")
    parser.add_argument("--coveragedir", dest="coveragedir",
                        help="Write tested RPC commands into this directory")
    parser.add_argument("--configfile", dest="configfile", default=os.path.abspath(os.path.dirname(os.path.realpath(
        __file__)) + "/../config.ini"), help="Location of the test framework config file (default: %(default)s)")
    parser.add_argument("--pdbonfailure", dest="pdbonfailure", default=False, action="store_true",
                        help="Attach a python debugger if test fails")
    parser.add_argument("--usecli", dest="usecli", default=False, action="store_true",
                        help="use bitcoin-cli instead of RPC for all commands")
    parser.add_argument("--with-gravitonactivation", dest="gravitonactivation", default=False, action="store_true",
                        help="Activate graviton update on timestamp {}".format(TIMESTAMP_IN_THE_PAST))
    parser.add_argument("--minport", dest="minport", default=test_framework.util.PORT_MIN, type=int,
                        help="Lowest port number available for use. Default: 11000")
    parser.add_argument("--portrange", dest="portrange", default=test_framework.util.PORT_RANGE, type=int,
                        help="Number of ports available for use for each of P2P and RPC. Default: 5000")
    cli_options = parser.parse_args()

    config = configparser.ConfigParser()
    config.read_file(open(cli_options.configfile))

    test_framework.util.PORT_RANGE = cli_options.portrange
    test_framework.util.PORT_MIN = cli_options.minport
    test_framework.util.MAX_NODES = int(cli_options.minport / 2)

    # if "options" in config:
    #     options = config["options"]
    # else:
    #     options = {}
    # print(options)
    # print(cli_options)
    # options.update(cli_options)
    return cli_options, config

class ResourceManager:
    def __init__(self):
        if sys.platform == 'linux':
            with open('/proc/cpuinfo', 'r') as f:
                cpuinfo = f.read()
                self.cores = cpuinfo.count('processor\t')
                self.model = cpuinfo.split('model name\t: ')[1].split('\n')[0]
        else:
            print("Platform '%s' is not yet supported. Try Linux?" % sys.platform)
        self.tests = {}
        self.options, self.config = parse_config_and_options()

    def add_test(self, testname, num_nodes, *args, **kwargs):
        assert type(num_nodes) == int and num_nodes > 0
        print("Starting test %i" % (len(self.tests) +1))
        mgr = NodeManager()
        self.tests[testname] = mgr
        mgr.set_test_params(testname, num_nodes, self.options, self.config, *args, **kwargs)
        mgr.main()
        return True
    def end_test(self, testname):
        if not testname in self.tests: 
            print("Could not end test that didn't exist")
            return False
        else:
            print("Ending test %s" % testname)
        self.tests[testname].finish()
        del self.tests[testname]
        return True

class NodeManager(BitcoinTestFramework):
    def set_test_params(self, testname, num_nodes, start_chain=True, xthinner='1', options=None, config=None, *args, **kwargs):
        self.start_chain = start_chain
        self.setup_clean_chain = True
        self.num_nodes = num_nodes
        if options == None or config == None:
            options, config = parse_config_and_options()
        self.options = options
        self.config = config
        self.extra_args = [["-usexthinner=%s"%xthinner, 
                            "-blockmaxsize=32000000", 
                            "-checkmempool=0", 
                            "-debug=net", 
                            "-debug=mempool"]]* self.num_nodes


        self.options.bitcoind = os.getenv("BITCOIND", 
            default=config["environment"]["BUILDDIR"] + '/src/bitcoind' + config["environment"]["EXEEXT"])
        self.options.bitcoincli = os.getenv("BITCOINCLI", 
            default=config["environment"]["BUILDDIR"] + '/src/bitcoin-cli' + config["environment"]["EXEEXT"])

        if self.options.tmpdir:
            self.options.tmpdir = os.path.abspath(self.options.tmpdir)
            os.makedirs(self.options.tmpdir, exist_ok=False)
        else:
            self.options.tmpdir = tempfile.mkdtemp(suffix=testname, prefix="test")
        if self.options.walletdir:
            self.options.walletdir = os.path.abspath(self.options.walletdir)
            os.makedirs(self.options.walletdir, exist_ok=False)
        self.bind_to_localhost_only = False


    def setup_chain(self):
        if self.start_chain:
            BitcoinTestFramework.setup_chain(self)
    def setup_network(self):
        self.setup_nodes()

    # def add_nodes(self, num_nodes, extra_args=None, rpchost=None, timewait=None, binary=None):
    #     extra_confs = [[]] * num_nodes
    #     if extra_args is None:
    #         extra_args = [[]] * num_nodes
    #     if binary is None:
    #         binary = [self.options.bitcoind] * num_nodes
    #     assert_equal(len(extra_confs), num_nodes)
    #     assert_equal(len(extra_args), num_nodes)
    #     assert_equal(len(binary), num_nodes)
    #     for i in range(num_nodes):
    #         self.nodes.append(TestNode(i, get_datadir_path(self.options.tmpdir, i), host=rpchost, rpc_port=rpc_port(i), p2p_port=p2p_port(i), timewait=timewait,
    #                                    bitcoind=binary[i], bitcoin_cli=self.options.bitcoincli, mocktime=self.mocktime, coverage_dir=self.options.coveragedir, extra_conf=extra_confs[i], extra_args=extra_args[i], use_cli=self.options.usecli,
    #                                    walletdir=get_datadir_path(self.options.walletdir, i) if self.options.walletdir else None))


    def main(self):

        assert hasattr(self, "num_nodes")

        PortSeed.n = self.options.port_seed

        os.environ['PATH'] = self.options.srcdir + ":" + \
            self.options.srcdir + "/qt:" + os.environ['PATH']

        check_json_precision()

        self.options.cachedir = os.path.abspath(self.options.cachedir)

        # Set up temp directory and start logging
        self._start_logging()

        success = TestStatus.FAILED

        try:
            if self.options.usecli and not self.supports_cli:
                raise SkipTest(
                    "--usecli specified but test does not support using CLI")
            self.setup_chain()
            self.setup_network()
        except JSONRPCException as e:
            self.log.exception("JSONRPC error")
        except SkipTest as e:
            self.log.warning("Test Skipped: {}".format(e.message))
            success = TestStatus.SKIPPED
        except AssertionError as e:
            self.log.exception("Assertion failed")
        except KeyError as e:
            self.log.exception("Key error")
        except Exception as e:
            self.log.exception("Unexpected exception caught during testing")
        except KeyboardInterrupt as e:
            self.log.warning("Exiting after keyboard interrupt")

    def finish(self):
        if not self.options.noshutdown:
            self.log.info("Stopping nodes")
            if self.nodes:
                self.stop_nodes()
        else:
            for node in self.nodes:
                node.cleanup_on_exit = False
            self.log.info(
                "Note: bitcoinds were not stopped and may still be running")

        if not self.options.nocleanup and not self.options.noshutdown:
            self.log.info("Cleaning up")
            shutil.rmtree(self.options.tmpdir)
            if self.options.walletdir: shutil.rmtree(self.options.walletdir)
        else:
            self.log.warning(
                "Not cleaning up dir {}".format(self.options.tmpdir))
        self._stop_logging()

    def run_test(self):
        # Setup the p2p connections and start up the network thread.
        #self.test_node = self.nodes[0].add_p2p_connection(TestNode())

        self.log.info("Running tests:")


@method
def getcpuinfo(testname=None):
    try:
        cpuinfo = "Unparseable CPU info"
        cpuinfo = str(subprocess.check_output('lscpu'))
        cpuinfo = cpuinfo.replace('\\n', '\n')
        sockets = int(cpuinfo.split("Socket(s):")[1].split('\n')[0].strip())
        threadspercore = int(cpuinfo.split("Thread(s) per core:")[1].split('\n')[0].strip())
        corespersocket = int(cpuinfo.split("Core(s) per socket:  ")[1].split('\n')[0].strip())
        model = cpuinfo.split("Model name:")[1].split('\n')[0].strip()
        MHz_now = float(cpuinfo.split("CPU MHz:")[1].split('\n')[0].strip())
        MHz_max = float(cpuinfo.split("CPU max MHz:")[1].split('\n')[0].strip())
        MHz_min = float(cpuinfo.split("CPU min MHz:")[1].split('\n')[0].strip())

        cores = sockets * corespersocket
        threads = cores * threadspercore
        return {"cores":cores, "threads":threads, "sockets":sockets, "model":model}
    except:
        traceback.print_exc()
        return {}

@method
def add_test(testname, num_nodes, *args, **kwargs):
    print("add_test called for test %s with num_nodes=" % testname, num_nodes, "args=", args, "kwargs=", kwargs)
    try:
        return resman.add_test(testname, num_nodes, *args, **kwargs)
    except:
        traceback.print_exc()
        raise
@method
def end_test(testname):
    try:
        return resman.end_test(testname)
    except:
        traceback.print_exc()
        raise
@method
def get_wan_ip(testname):
    try:
        if hasattr(resman.options, 'wan_ip'):
            return resman.options.wan_ip
        else:
            return requests.get('https://api.ipify.org').text
    except:
        traceback.print_exc()
        raise



@method
def get_node_p2p_ports(testname):
    try:
        return [node.p2p_port for node in resman.tests[testname].nodes]
    except:
        traceback.print_exc()
        raise
@method
def get_node_rpc_ports(testname):
    try:
        return [node.rpc_port for node in resman.tests[testname].nodes]
    except:
        traceback.print_exc()
        raise

@method
def get_many_addresses(testname, num_addresses):
    try:
        t0 = time.time()
        print('get_many_addresses', testname, num_addresses)
        node_addresses = [[] for _ in resman.tests[testname].nodes]
        def get_addresses(node, addresslist, n):
            for _ in range(n):
                addresslist.append(node.getnewaddress())
        threads = [threading.Thread(target=get_addresses, 
                                    args=(resman.tests[testname].nodes[i], 
                                          node_addresses[i], 
                                          num_addresses)) 
                   for i in range(len(resman.tests[testname].nodes))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        print("get_many_addresses took %5.3f sec" % (time.time()-t0))
        return node_addresses
    except:
        traceback.print_exc()
        raise

@method
def send_node_command(testname, nodenum, command, *args, **kwargs):
    def truncate(obj, size=75):
        return str(obj)[:size] + '...' if len(str(obj)) > size else ''
    try:
        print(testname, nodenum, command, truncate(args), truncate(kwargs))
        return getattr(resman.tests[testname].nodes[nodenum],
                       command)(*args, **kwargs)
    except:
        traceback.print_exc()
        raise

class TestHttpServer(BaseHTTPRequestHandler):
    def do_POST(self):
        # Process request
        request = self.rfile.read(int(self.headers["Content-Length"])).decode()
        response = dispatch(request)
        # Return response
        self.send_response(response.http_status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(str(response).encode())
    def log_error(self, *args, **kwargs):
        return BaseHTTPRequestHandler.log_error(self, *args, **kwargs)
    def log_message(self, *args, **kwargs):
        return

if __name__ == "__main__":
    resman = ResourceManager()
    #serve()
    HTTPServer(("", resman.options.minport-1), TestHttpServer).serve_forever()
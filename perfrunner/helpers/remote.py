import time

from decorator import decorator
from fabric import state
from fabric.api import execute, get, parallel, put, run, settings
from fabric.exceptions import CommandTimeout
from logger import logger

from perfrunner.helpers.misc import uhex
from random import uniform

@decorator
def all_hosts(task, *args, **kargs):
    self = args[0]
    return execute(parallel(task), *args, hosts=self.hosts, **kargs)


@decorator
def single_host(task, *args, **kargs):
    self = args[0]
    with settings(host_string=self.hosts[0]):
        return task(*args, **kargs)


@decorator
def single_client(task, *args, **kargs):
    self = args[0]
    with settings(host_string=self.cluster_spec.workers[0]):
        return task(*args, **kargs)


@decorator
def all_clients(task, *args, **kargs):
    self = args[0]
    return execute(parallel(task), *args, hosts=self.cluster_spec.workers, **kargs)


@decorator
def seriesly_host(task, *args, **kargs):
    self = args[0]
    with settings(host_string=self.test_config.gateload_settings.seriesly_host):
        return task(*args, **kargs)


@decorator
def all_gateways(task, *args, **kargs):
    self = args[0]
    return execute(parallel(task), *args, hosts=self.gateways, **kargs)


@decorator
def all_gateloads(task, *args, **kargs):
    self = args[0]
    return execute(parallel(task), *args, hosts=self.gateloads, **kargs)


@decorator
def all_kv_nodes(task, *args, **kargs):
    self = args[0]
    self.host_index = 0
    return execute(parallel(task), *args, hosts=self.kv_hosts, **kargs)


@decorator
def kv_node(task, *args, **kargs):
    self = args[0]
    host = self.cluster_spec.yield_kv_servers().next()
    with settings(host_string=host):
        return task(*args, **kargs)


class RemoteHelper(object):

    def __new__(cls, cluster_spec, test_config, verbose=False):
        if not cluster_spec.ssh_credentials:
            return None

        state.env.user, state.env.password = cluster_spec.ssh_credentials
        state.output.running = verbose
        state.output.stdout = verbose

        os = cls.detect_os(cluster_spec)
        if os == 'Cygwin':
            return RemoteWindowsHelper(cluster_spec, test_config, os)
        else:
            return RemoteLinuxHelper(cluster_spec, test_config, os)

    @staticmethod
    def detect_os(cluster_spec):
        logger.info('Detecting OS')
        with settings(host_string=cluster_spec.yield_hostnames().next()):
            os = run('python -c "import platform; print platform.dist()[0]"',
                     pty=True)
        if os:
            return os
        else:
            return 'Cygwin'

current_host_index = -1


class CurrentHostMutex:

    def __init__(self):
        f = open("/tmp/current_host.txt", 'w')
        f.write("0")
        f.close()

    @staticmethod
    def next_host_index():
        f = open("/tmp/current_host.txt", 'r')
        next_host = f.readline()
        f.close()
        f = open("/tmp/current_host.txt", 'w')
        f.write((int(next_host) + 1).__str__())
        return int(next_host)


class RemoteLinuxHelper(object):

    ARCH = {'i386': 'x86', 'x86_64': 'x86_64', 'unknown': 'x86_64'}

    CB_DIR = '/opt/couchbase'
    MONGO_DIR = '/opt/mongodb'
    INBOX_FOLDER = "inbox"

    PROCESSES = ('beam.smp', 'memcached', 'epmd', 'cbq-engine', 'mongod', 'indexer',
                 'cbft', 'goport', 'goxdcr', 'couch_view_index_updater', 'moxi', 'spring')

    def __init__(self, cluster_spec, test_config, os):
        self.os = os
        self.hosts = tuple(cluster_spec.yield_hostnames())
        self.kv_hosts = tuple(cluster_spec.yield_kv_servers())
        self.current_host = CurrentHostMutex()
        self.cluster_spec = cluster_spec
        self.test_config = test_config
        self.env = {}

        if self.cluster_spec.gateways and test_config is not None:
            num_nodes = self.test_config.gateway_settings.num_nodes
            self.gateways = self.cluster_spec.gateways[:num_nodes]
            self.gateloads = self.cluster_spec.gateloads[:num_nodes]
        else:
            self.gateways = self.gateloads = None

    @staticmethod
    def wget(url, outdir='/tmp', outfile=None):
        logger.info('Fetching {}'.format(url))
        if outfile is not None:
            run('wget -nc "{}" -P {} -O {}'.format(url, outdir, outfile))
        else:
            run('wget -N "{}" -P {}'.format(url, outdir))

    @single_host
    def detect_pkg(self):
        logger.info('Detecting package manager')
        if self.os.upper() in ('UBUNTU', 'DEBIAN'):
            return 'deb'
        else:
            return 'rpm'

    @single_host
    def detect_arch(self):
        logger.info('Detecting platform architecture')
        arch = run('uname -i', pty=True)
        return self.ARCH[arch]

    @single_host
    def build_secondary_index(self, index_nodes, bucket, indexes, fields,
                              secondarydb, where_map, command_path='/opt/couchbase/bin/'):
        logger.info('building secondary indexes')

        # Remember what bucket:index was created
        bucket_indexes = []

        for index, field in zip(indexes, fields):
            cmd = command_path + "cbindex"
            cmd += ' -auth=Administrator:password'
            cmd += ' -server {}'.format(index_nodes[0])
            cmd += ' -type create -bucket {}'.format(bucket)
            cmd += ' -fields={}'.format(field)

            if secondarydb:
                cmd += ' -using {}'.format(secondarydb)

            if index in where_map and field in where_map[index]:
                # Partition indexes over index nodes by deploying index with
                # where clause on the corresponding index node
                where_list = where_map[index][field]
                for i, (index_node, where) in enumerate(
                        zip(index_nodes, where_list)):
                    # don't taint cmd itself because we need to reuse it.
                    final_cmd = cmd
                    index_i = index + "_{}".format(i)
                    final_cmd += ' -index {}'.format(index_i)
                    final_cmd += " -where='{}'".format(where)

                    # Since .format() is sensitive to {}, use % formatting
                    with_str_template = \
                        r'{\\\"defer_build\\\":true, \\\"nodes\\\":[\\\"%s\\\"]}'
                    with_str = with_str_template % index_node

                    final_cmd += ' -with=\\\"{}\\\"'.format(with_str)
                    bucket_indexes.append("{}:{}".format(bucket, index_i))
                    logger.info('submitting cbindex command {}'.format(final_cmd))
                    status = run(final_cmd, shell_escape=False, pty=False)
                    if status:
                        logger.info('cbindex status {}'.format(status))
            else:
                # no partitions, no where clause
                final_cmd = cmd
                final_cmd += ' -index {}'.format(index)
                with_str = r'{\\\"defer_build\\\":true}'
                final_cmd += ' -with=\\\"{}\\\"'.format(with_str)
                bucket_indexes.append("{}:{}".format(bucket, index))

                logger.info('submitting cbindex command {}'.format(final_cmd))
                status = run(final_cmd, shell_escape=False, pty=False)
                if status:
                    logger.info('cbindex status {}'.format(status))

        time.sleep(10)

        # build indexes
        cmdstr = command_path + 'cbindex -auth="Administrator:password"'
        cmdstr += ' -server {}'.format(index_nodes[0])
        cmdstr += ' -type build'
        cmdstr += ' -indexes {}'.format(",".join(bucket_indexes))
        logger.info('cbindex build command {}'.format(cmdstr))
        status = run(cmdstr, shell_escape=False, pty=False)
        if status:
            logger.info('cbindex status {}'.format(status))

    @all_kv_nodes
    def run_spring_on_kv(self, creates=0, reads=0, updates=0, deletes=0, expires=0, operations=float('inf'),
                         throughput=float('inf'), size=2048, existing_items=0, items_in_working_set=100,
                         operations_to_hit_working_set=100, workers=1, silent=False):
        logger.info("running spring on kv nodes")
        number_of_kv_nodes = self.kv_hosts.__len__()
        existing_item = operation = 0
        sleep_time = uniform(1, number_of_kv_nodes)
        time.sleep(sleep_time)
        host = self.current_host.next_host_index()
        logger.info("current_host_index {}".format(host))
        if host != number_of_kv_nodes - 1:
            if (creates != 0 or reads != 0 or updates != 0 or deletes != 0) and operations != float('inf'):
                operation = int(operations / (number_of_kv_nodes * 100)) * 100
                if creates == 100:
                    existing_item = operation * host + existing_items
                else:
                    existing_item = existing_items
        else:
            if (creates != 0 or reads != 0 or updates != 0 or deletes != 0) and operations != float('inf'):
                operation = operations - (int(operations / (number_of_kv_nodes * 100)) * 100 * (number_of_kv_nodes - 1))
                if creates == 100:
                    existing_item = operation * host + existing_items
                else:
                    existing_item = existing_items
                f = open("/tmp/current_host.txt", "w")
                f.write("0")
                f.close()
        time.sleep(number_of_kv_nodes * 2 - sleep_time)
        cmdstr = "spring -c {} -r {} -u {} -d {} -e {} " \
                 "-s {} -i {} -w {} -W {} -n {}".format(creates, reads, updates, deletes, expires, size,
                                                        existing_item, items_in_working_set,
                                                        operations_to_hit_working_set, workers, self.hosts[0])
        if operation != 0:
            cmdstr += " -o {}".format(operation)
        if throughput != float('inf'):
            thrput = int(throughput / (number_of_kv_nodes * 100)) * 100
            cmdstr += " -t {}".format(thrput)
        cmdstr += " cb://Administrator:password@{}:8091/bucket-1".format(self.hosts[0])
        pty = True
        if silent:
            cmdstr += " >/tmp/springlog.log 2>&1 &"
            pty = False
        logger.info(cmdstr)
        result = run(cmdstr, pty=pty)
        if silent:
            time.sleep(10)
            res = run(r"ps aux | grep -ie spring | awk '{print $11}'")
            if "python2.7" not in res:
                raise Exception("Spring not run on KV. {}".format(res))
        else:
            if "Current progress: 100.00 %" not in result and "Finished: worker-{}".format(workers - 1) not in result:
                raise Exception("Spring not run on KV")

    @all_kv_nodes
    def check_spring_running(self):
        cmdstr = r"ps aux | grep -ie spring | awk '{print $11}'"
        result = run(cmdstr)
        logger.info(result)
        logger.info(result.stdout)

    @all_kv_nodes
    def kill_spring_processes(self):
        cmdstr = "ps aux | grep -ie spring | awk '{print $2}' | xargs kill -9"
        result = run(cmdstr, quiet=True)
        if result.failed:
            pass

    @single_host
    def run_cbindexperf(self, index_host, config):
        rest_username, rest_password = self.cluster_spec.rest_credentials
        put(config, "/tmp/config.json")
        configfile = "/tmp/config.json"
        cmdstr = "/opt/couchbase/bin/cbindexperf -cluster {} -auth=\"{}:{}\" -configfile {} -resultfile " \
                 "/root/result.json -statsfile /root/statsfile".format(index_host, rest_username, rest_password,
                                                                       configfile)
        logger.info("Running cbindexperf: {}".format(cmdstr))
        result = run(cmdstr)
        if "Throughput = " not in result:
            raise Exception('Scan workload could not be applied')
        logger.info(result)

    @single_host
    def get_files_from_host(self, remotepath, localpath):
        result = get(remotepath, localpath)
        if result.failed:
            raise Exception("Could not copy {} from remote to {} local".format(remotepath, localpath))

    @single_host
    def set_dcp_io_threads(self):
        cmdstr = "curl -u Administrator:password -XPOST -d 'ns_bucket:update_bucket_props(\"bucket-1\", " \
                 "[{extra_config_string, \"max_num_auxio=16\"}])'"
        cmdstr += " http://{}:8091/diag/eval".format(self.hosts[0])
        logger.info("Changing the DCP IO threads")
        logger.info(cmdstr)
        run(cmdstr)

    @single_host
    def detect_openssl(self, pkg):
        logger.info('Detecting openssl version')
        if pkg == 'rpm':
            return run('rpm -q --qf "%{VERSION}" openssl.x86_64')

    @all_hosts
    def setup_master_ca_cert(self, src_chain_file, dest_chain_folder):
        path_to_root_cert = dest_chain_folder + "root.crt"
        run('mkdir -p {}'.format(dest_chain_folder))
        put(src_chain_file + "root.crt", path_to_root_cert)
        run('/opt/couchbase/bin/couchbase-cli ssl-manage --cluster=localhost -u Administrator -p password --upload-cluster-ca={}/root.crt'.format(dest_chain_folder))

    @all_hosts
    def reset_swap(self):
        logger.info('Resetting swap')
        run('swapoff --all && swapon --all')

    @all_hosts
    def drop_caches(self):
        logger.info('Dropping memory cache')
        run('sync && echo 3 > /proc/sys/vm/drop_caches')

    @all_hosts
    def set_swappiness(self):
        logger.info('Changing swappiness to 0')
        run('sysctl vm.swappiness=0')

    @all_hosts
    def disable_thp(self):
        for path in (
            '/sys/kernel/mm/transparent_hugepage/enabled',
            '/sys/kernel/mm/redhat_transparent_hugepage/enabled',
        ):
            run('echo never > {}'.format(path), quiet=True)

    @all_hosts
    def collect_info(self):
        logger.info('Running cbcollect_info')

        run('rm -f /tmp/*.zip')

        fname = '/tmp/{}.zip'.format(uhex())
        try:
            r = run('{}/bin/cbcollect_info {}'.format(self.CB_DIR, fname),
                    warn_only=True, timeout=1200)
        except CommandTimeout:
            logger.error('cbcollect_info timed out')
            return
        if not r.return_code:
            get('{}'.format(fname))
            run('rm -f {}'.format(fname))

    @all_hosts
    def clean_data(self):
        for path in self.cluster_spec.paths:
            run('rm -fr {}/*'.format(path))
        run('rm -fr {}'.format(self.CB_DIR))

    @all_hosts
    def kill_processes(self):
        logger.info('Killing {}'.format(', '.join(self.PROCESSES)))
        run('killall -9 {}'.format(' '.join(self.PROCESSES)),
            warn_only=True, quiet=True)

    @all_hosts
    def uninstall_couchbase(self, pkg):
        logger.info('Uninstalling Couchbase Server')
        if pkg == 'deb':
            run('yes | apt-get remove couchbase-server', quiet=True)
            run('yes | apt-get remove couchbase-server-community', quiet=True)
        else:
            run('yes | yum remove couchbase-server', quiet=True)
            run('yes | yum remove couchbase-server-community', quiet=True)

    @all_hosts
    def install_couchbase(self, pkg, url, filename, version=None):
        self.wget(url, outdir='/tmp')

        logger.info('Installing Couchbase Server')
        if pkg == 'deb':
            run('yes | apt-get install gdebi')
            run('yes | numactl --interleave=all gdebi /tmp/{}'.format(filename))
        else:
            run('yes | numactl --interleave=all rpm -i /tmp/{}'.format(filename))

    @all_hosts
    def restart(self):
        logger.info('Restarting server')
        environ = ' '.join('{}={}'.format(k, v) for (k, v) in self.env.items())
        run(environ +
            ' numactl --interleave=all /etc/init.d/couchbase-server restart',
            pty=False)

    def restart_with_alternative_num_vbuckets(self, num_vbuckets):
        logger.info('Changing number of vbuckets to {}'.format(num_vbuckets))
        self.env['COUCHBASE_NUM_VBUCKETS'] = num_vbuckets
        self.restart()

    def restart_with_alternative_num_cpus(self, num_cpus):
        logger.info('Changing number of front-end memcached threads to {}'
                    .format(num_cpus))
        self.env['MEMCACHED_NUM_CPUS'] = num_cpus
        self.restart()

    def restart_with_sfwi(self):
        logger.info('Enabling +sfwi')
        self.env['COUCHBASE_NS_SERVER_VM_EXTRA_ARGS'] = '["+sfwi", "100", "+sbwt", "long"]'
        self.restart()

    def restart_with_tcmalloc_aggressive_decommit(self):
        logger.info('Enabling TCMalloc aggressive decommit')
        self.env['TCMALLOC_AGGRESSIVE_DECOMMIT'] = 't'
        self.restart()

    @all_hosts
    def disable_moxi(self):
        logger.info('Disabling moxi')
        run('rm /opt/couchbase/bin/moxi')
        run('killall -9 moxi')

    @all_hosts
    def stop_server(self):
        logger.info('Stopping Couchbase Server')
        getosname = run('uname -a|cut -c1-6')
        if(getosname.find("CYGWIN") != -1):
            run('net stop CouchbaseServer')
        else:
            run('/etc/init.d/couchbase-server stop', pty=False)

    @all_hosts
    def start_server(self):
        logger.info('Starting Couchbase Server')
        getosname = run('uname -a|cut -c1-6')
        if(getosname.find("CYGWIN") != -1):
            run('net start CouchbaseServer')
        else:
            run('/etc/init.d/couchbase-server start', pty=False)

    def detect_if(self):
        for iface in ('em1', 'eth5', 'eth0'):
            result = run('grep {} /proc/net/dev'.format(iface),
                         warn_only=True, quiet=True)
            if not result.return_code:
                return iface

    def detect_ip(self, _if):
        ifconfig = run('ifconfig {} | grep "inet addr"'.format(_if), warn_only=True)
        if not ifconfig:
            ifconfig = run('ifconfig | grep "inet addr"| grep Bcast')
        return ifconfig.split()[1].split(':')[1]

    @all_hosts
    def disable_wan(self):
        logger.info('Disabling WAN effects')
        _if = self.detect_if()
        run('tc qdisc del dev {} root'.format(_if), warn_only=True, quiet=True)

    @all_hosts
    def enable_wan(self):
        logger.info('Enabling WAN effects')
        _if = self.detect_if()
        for cmd in (
            'tc qdisc add dev {} handle 1: root htb',
            'tc class add dev {} parent 1: classid 1:1 htb rate 1gbit',
            'tc class add dev {} parent 1:1 classid 1:11 htb rate 1gbit',
            'tc qdisc add dev {} parent 1:11 handle 10: netem delay 40ms 2ms '
            'loss 0.005% 50% duplicate 0.005% corrupt 0.005%',
        ):
            run(cmd.format(_if))

    @all_hosts
    def filter_wan(self, src_list, dest_list):
        logger.info('Filtering WAN effects')
        _if = self.detect_if()

        if self.detect_ip(_if) in src_list:
            _filter = dest_list
        else:
            _filter = src_list

        for ip in _filter:
            run('tc filter add dev {} protocol ip prio 1 u32 '
                'match ip dst {} flowid 1:11'.format(_if, ip))

    @single_host
    def detect_number_cores(self):
        logger.info('Detecting number of cores')
        return int(run('nproc', pty=False))

    @all_hosts
    def detect_core_dumps(self):
        # Based on kernel.core_pattern = /tmp/core.%e.%p.%h.%t
        r = run('ls /tmp/core*', quiet=True)
        if not r.return_code:
            return r.split()
        else:
            return []

    @all_hosts
    def tune_log_rotation(self):
        logger.info('Tune log rotation so that it happens less frequently')
        run('sed -i "s/num_files, [0-9]*/num_files, 50/" '
            '/opt/couchbase/etc/couchbase/static_config')

    @all_hosts
    def start_cbq(self):
        logger.info('Starting cbq-engine')
        return run('nohup cbq-engine '
                   '-couchbase=http://127.0.0.1:8091 -dev=true -log=HTTP '
                   '&> /tmp/cbq.log &', pty=False)

    @all_hosts
    def collect_cbq_logs(self):
        logger.info('Getting cbq-engine logs')
        get('/tmp/cbq.log')

    @all_hosts
    def delete_inbox_folder(self):
        final_path = self.CB_DIR + self.INBOX_FOLDER
        run('rm -rf ' + final_path)

    @all_hosts
    def setup_cluster_nodes(self, dest_chain_folder):
        run('mkdir -p {}'.format(dest_chain_folder))
        local_ip = self.detect_ip(self.detect_if())
        src_chain_file = "/tmp/newcerts/long_chain" + local_ip + ":8091.pem"
        dest_chain_file = dest_chain_folder + "chain.pem"
        put(src_chain_file, dest_chain_file)
        src_node_key = "/tmp/newcerts/" + local_ip + ":8091.key"
        dest_node_key = dest_chain_folder + "pkey.pem"
        put(src_node_key, dest_node_key)
        run('/opt/couchbase/bin/couchbase-cli ssl-manage --cluster=localhost -u Administrator -p password --set-node-certificate')

    @all_clients
    def cbbackup(self, wrapper=False, mode=None):  # full, diff, accu
        backup_path = self.cluster_spec.config.get('storage', 'backup_path')
        logger.info('cbbackup into %s' % backup_path)
        postfix = ''
        if mode:
            postfix = '-m %s' % mode
        if not mode or mode in ['full']:
            run('rm -rf %s' % backup_path)
        start = time.time()
        if wrapper:
            for master in self.cluster_spec.yield_masters():
                cmd = 'cd /opt/couchbase/bin && ./cbbackupwrapper' \
                      ' http://%s:8091 %s -u %s -p %s -P 16 %s' \
                      % (master.split(':')[0], backup_path,
                         self.cluster_spec.rest_credentials[0],
                         self.cluster_spec.rest_credentials[1], postfix)
                logger.info(cmd)
                run(cmd)
        else:
            for master in self.cluster_spec.yield_masters():
                if not mode:
                    run('/opt/couchbase/bin/cbbackupmgr config --archive %s --repo default' % backup_path)
                # EE backup does not support modes, ignore 'full, diff, accu'
                cmd = '/opt/couchbase/bin/cbbackupmgr backup --archive %s --repo default ' \
                    '--host http://%s:8091 --username %s --password %s --threads 16' \
                    % (backup_path, master.split(':')[0],
                        self.cluster_spec.rest_credentials[0],
                        self.cluster_spec.rest_credentials[1])
                logger.info(cmd)
                run(cmd)
        delta_time = int(time.time() - start)
        return (round(float(run('du -sh --block-size=1M %s' % backup_path).
                split('	')[0]) / 1024, 1), delta_time)  # in Gb / sec

    @all_clients
    def cbrestore(self, wrapper=False):
        restore_path = self.cluster_spec.config.get('storage', 'backup_path')
        logger.info('restore from %s' % restore_path)
        start = time.time()
        if wrapper:
            for master in self.cluster_spec.yield_masters():
                cmd = 'cd /opt/couchbase/bin && ./cbrestorewrapper %s ' \
                    'http://%s:8091 -u Administrator -p password' \
                    % (restore_path, master.split(':')[0])
                run(cmd)
        else:
            for master in self.cluster_spec.yield_masters():
                dates = run('ls %s/default/ | grep 20' % restore_path).split()
                for i in range(len(dates)):
                    start_date = end_date = dates[i]
                    if i < len(dates) - 1:
                        end_date = dates[i + 1]
                    cmd = '/opt/couchbase/bin/cbbackupmgr restore --archive %s --repo default ' \
                        '--host http://%s:8091 --username %s --password %s --start %s --end %s ' \
                        '--threads 16' % (restore_path, master.split(':')[0],
                                          self.cluster_spec.rest_credentials[0],
                                          self.cluster_spec.rest_credentials[1],
                                          start_date, end_date)
                    run(cmd)
        return int(time.time() - start)

    @single_host
    def cbrestorefts(self):
        restore_path = self.cluster_spec.config.get('storage', 'backup_path')
        logger.info('restore from %s' % restore_path)
        cmd = "cd /opt/couchbase/bin && ./cbrestorewrapper {}  http://127.0.0.1:8091 " \
              "-b {} -u Administrator -p password".format(restore_path, self.test_config.buckets[0])
        run(cmd)

    @single_host
    def startelasticsearchplugin(self):
        cmd = ['service elasticsearch restart',
               'service elasticsearch stop',
               'sudo chkconfig --add elasticsearch',
               'sudo service elasticsearch restart',
               'sudo service elasticsearch stop',
               'cd /usr/share/elasticsearch',
               'echo "couchbase.password: password" >> /etc/elasticsearch/elasticsearch.yml',
               '/usr/share/elasticsearch/bin/plugin  -install transport-couchbase -url http://packages.couchbase.com.s3.amazonaws.com/releases/elastic-search-adapter" \
                                                      "/2.0.0/elasticsearch-transport-couchbase-2.0.0.zip',
               'echo "couchbase.password: password" >> /etc/elasticsearch/elasticsearch.yml',
               'echo "couchbase.username: Administrator" >> /etc/elasticsearch/elasticsearch.yml'
               'bin/plugin -install mobz/elasticsearch-head',
               'sudo service elasticsearch restart']

        for c in cmd.split:
            cmd = c.strip()
            logger.info("command executed {}".format(cmd))
            run(cmd)

    @single_client
    def generate_certs(self, root_cn='Root\ Authority', type='go',
                       encryption="", key_length=1024):
        cert_folder = "/tmp/newcerts/"
        for _, master in zip(self.cluster_spec.workers,
                             self.cluster_spec.yield_masters()):
            for bucket in self.test_config.buckets:
                qname = '{}-{}'.format(master.split(':')[0], bucket)
                temp_dir = '{}-{}'.format(
                    self.test_config.worker_settings.worker_dir, qname)

        if type == 'go':
            cert_file = "{}/perfrunner/scripts/security/gencert.go".\
                format(temp_dir)
            run("rm -rf {}".format(cert_folder))
            run("mkdir {}".format(cert_folder))

            run("go run {} -store-to=/tmp/newcerts/root -common-name={}".
                format(cert_file, root_cn))
            run("go run {} -store-to={}/interm -sign-with={}/root "
                "-common-name=Intemediate\ Authority".format(
                    cert_file, cert_folder, cert_folder))
            for _, servers in self.cluster_spec.yield_clusters():
                for server in servers:
                    run("go run {} -store-to={}{} -sign-with={}interm "
                        "-common-name={} -final=true".format(
                            cert_file, cert_folder, server, cert_folder, server))
                    run("cat {}{}.crt {}interm.crt > {}/long_chain{}.pem".
                        format(cert_folder, server, cert_folder,
                               cert_folder, server))
        elif type == 'openssl':
            v3_ca = "./pytests/security/v3_ca.crt"
            run("rm -rf /tmp/newcerts")
            run("mkdir /tmp/newcerts")
            run("openssl genrsa {} -out {}ca.key {}".format(
                encryption, cert_folder, str(key_length)))
            run("openssl req -new -x509  -days 3650 -sha256 -key {}ca.key -out"
                " /tmp/newcerts/ca.pem -subj '/C=UA/O=My "
                "Company/CN=My Company Root CA'".format(cert_folder))
            run("openssl genrsa {} -out {}/int.key {}".format(
                encryption, cert_folder, str(key_length)))
            run("openssl req -new -key {}int.key -out {}/int.csr -subj "
                "'/C=UA/O=My Company/CN=My Company Intermediate CA'".
                format(cert_folder, cert_folder))
            run("openssl x509 -req -in {}int.csr -CA {}ca.pem -CAkey {}ca.key "
                "-CAcreateserial -CAserial {}rootCA.srl -extfile {} "
                "-out {}int.pem -days 365 -sha256".
                format(cert_folder, cert_folder, cert_folder,
                       cert_folder, v3_ca, cert_folder))
            for _, servers in self.cluster_spec.yield_clusters():
                for server in servers:
                    run("openssl genrsa {} -out {}{}.key {}".format(
                        encryption, cert_folder, server, str(key_length)))
                    run("openssl req -new -key {}{}.key -out {}{}.csr -subj "
                        "'/C=UA/O=My Company/CN={}'".format(
                            cert_folder, server, cert_folder, server, server))
                    run("openssl x509 -req -in {}{}.csr -CA {}int.pem -CAkey"
                        " {}int.key -CAcreateserial -CAserial {}intermediateCA.srl "
                        "-out .pem -days 365 -sha256".format(
                            cert_folder, server, cert_folder, cert_folder,
                            cert_folder, cert_folder, server))
                    run("openssl x509 -req -days 300 -in {}{}.csr -CA {}int.pem "
                        "-CAkey {}int.key -set_serial 01 -out {}{}.pem".format(
                            cert_folder, server, cert_folder,
                            cert_folder, cert_folder, server))
                    run("cat {}{}.pem {}int.pem > {}long_chain{}.pem".format(
                        cert_folder, server, cert_folder, cert_folder, server))

    @single_client
    def copy_folder_locally(self, src_folder='/tmp/newcerts/', dest_folder='/tmp/newcerts/'):
        get(src_folder, dest_folder)

    @all_hosts
    def start_bandwidth_monitor(self, track_time=1):
        """Run iptraf to generate various network statistics and sent output to log file
        track_time tells IPTraf to run the specified facility for only timeout minutes.
        """
        kill_command = "pkill -9 iptraf; rm -rf /tmp/iptraf.log; rm -rf /var/lock/iptraf/*; " \
                       "rm -rf /var/log/iptraf/*"
        start_command = "sudo iptraf -i eth0 -L /tmp/iptraf.log -t %d -B /dev/null" % track_time
        for i in range(2):
            run(kill_command)
            run(start_command)
            time.sleep(2)
            res = run('ps -ef| grep "iptraf" | grep tmp')
            logger.info('print res', res)
            if not res:
                time.sleep(2)
            else:
                break

    @all_hosts
    def read_bandwidth_stats(self, type, servers):
        result = 0
        for server in servers:
            server_ip = server.split(':')[0]
            command = "cat /tmp/iptraf.log | grep 'FIN sent' | grep '" + type + " " + server_ip + ":11210'"
            logger.info(run(command, quiet=True))
            command += "| awk 'BEGIN{FS = \";\"}{if ($0 ~ /packets/) {if ($6 ~ /FIN sent/)" \
                       " {print $7}}}' | awk '{print $3}'"
            temp = run(command, quiet=True)
            if temp:
                arr = [int(t) for t in temp.split("\r\n")]
                result += int(sum(arr))
            time.sleep(3)
        return result

    @all_hosts
    def kill_process(self, process=''):
        command = "pkill -9 %s" % process
        run(command, quiet=True)

    @seriesly_host
    def restart_seriesly(self):
        logger.info('Cleaning up and restarting seriesly')
        run('killall -9 sample seriesly', quiet=True)
        run('rm -f *.txt *.log *.gz *.json *.out /root/seriesly-data/*',
            warn_only=True)
        run('nohup seriesly -flushDelay=1s -root=/root/seriesly-data '
            '&> seriesly.log &', pty=False)

    @seriesly_host
    def start_sampling(self):
        for i, gateway_ip in enumerate(self.gateways, start=1):
            logger.info('Starting sampling gateway_{}'.format(i))
            run('nohup sample -v '
                'http://{}:4985/_expvar http://localhost:3133/gateway_{} '
                '&> sample.log &'.format(gateway_ip, i), pty=False)
        for i, gateload_ip in enumerate(self.gateloads, start=1):
            logger.info('Starting sampling gateload_{}'.format(i))
            run('nohup sample -v '
                'http://{}:9876/debug/vars http://localhost:3133/gateload_{} '
                '&> sample.log &'.format(gateload_ip, i), pty=False)

    @all_gateways
    def install_gateway(self, url, filename):
        logger.info('Installing Sync Gateway package - {}'.format(filename))
        self.wget(url, outdir='/tmp')
        run('yes | rpm -i /tmp/{}'.format(filename))

    @all_gateways
    def install_gateway_from_source(self, commit_hash):
        logger.info('Installing Sync Gateway from source - {}'.format(commit_hash))
        put('scripts/install_sgw_from_source.sh', '/root/install_sgw_from_source.sh')
        run('chmod 777 /root/install_sgw_from_source.sh')
        run('/root/install_sgw_from_source.sh {}'.format(commit_hash), pty=False)

    @all_gateways
    def uninstall_gateway(self):
        logger.info('Uninstalling Sync Gateway package')
        run('yes | yum remove couchbase-sync-gateway')

    @all_gateways
    def kill_processes_gateway(self):
        logger.info('Killing Sync Gateway')
        run('killall -9 sync_gateway sgw_test_info.sh sar', quiet=True)

    @all_gateways
    def clean_gateway(self):
        logger.info('Cleaning up Gateway')
        run('rm -f *.txt *.log *.gz *.json *.out *.prof', quiet=True)

    @all_gateways
    def start_gateway(self):
        logger.info('Starting Sync Gateway instances')
        _if = self.detect_if()
        local_ip = self.detect_ip(_if)
        index = self.gateways.index(local_ip)
        source_config = 'templates/gateway_config_{}.json'.format(index)
        put(source_config, '/root/gateway_config.json')
        godebug = self.test_config.gateway_settings.go_debug
        args = {
            'ulimit': 'ulimit -n 65536',
            'godebug': godebug,
            'sgw': '/opt/couchbase-sync-gateway/bin/sync_gateway',
            'config': '/root/gateway_config.json',
            'log': '/root/gateway.log',
        }
        command = '{ulimit}; GODEBUG={godebug} nohup {sgw} {config} > {log} 2>&1 &'.format(**args)
        logger.info("Command: {}".format(command))
        run(command, pty=False)

    @all_gateways
    def start_test_info(self):
        logger.info('Starting Sync Gateway sgw_test_info.sh')
        put('scripts/sgw_test_config.sh', '/root/sgw_test_config.sh')
        put('scripts/sgw_test_info.sh', '/root/sgw_test_info.sh')
        run('chmod 777 /root/sgw_*.sh')
        run('nohup /root/sgw_test_info.sh &> sgw_test_info.txt &', pty=False)

    @all_gateways
    def collect_info_gateway(self):
        _if = self.detect_if()
        local_ip = self.detect_ip(_if)
        index = self.gateways.index(local_ip)
        logger.info('Collecting diagnostic information from sync gateway_{} {}'.format(index, local_ip))
        run('rm -f gateway.log.gz', warn_only=True)
        run('gzip gateway.log', warn_only=True)
        put('scripts/sgw_check_logs.sh', '/root/sgw_check_logs.sh')
        run('chmod 777 /root/sgw_*.sh')
        run('/root/sgw_check_logs.sh gateway > sgw_check_logs.out', warn_only=True)
        self.try_get('gateway.log.gz', 'gateway.log_{}.gz'.format(index))
        self.try_get('test_info.txt', 'test_info_{}.txt'.format(index))
        self.try_get('test_info_sar.txt', 'test_info_sar_{}.txt'.format(index))
        self.try_get('sgw_test_info.txt', 'sgw_test_info_{}.txt'.format(index))
        self.try_get('gateway_config.json', 'gateway_config_{}.json'.format(index))
        self.try_get('sgw_check_logs.out', 'sgw_check_logs_gateway_{}.out'.format(index))

    @all_gateloads
    def uninstall_gateload(self):
        logger.info('Removing Gateload binaries')
        run('rm -f /opt/gocode/bin/gateload', quiet=True)

    @all_gateloads
    def install_gateload(self):
        logger.info('Installing Gateload')
        run('go get -u github.com/couchbaselabs/gateload')

    @all_gateloads
    def kill_processes_gateload(self):
        logger.info('Killing Gateload processes')
        run('killall -9 gateload', quiet=True)

    @all_gateloads
    def clean_gateload(self):
        logger.info('Cleaning up Gateload')
        run('rm -f *.txt *.log *.gz *.json *.out', quiet=True)

    @all_gateloads
    def start_gateload(self):
        logger.info('Starting Gateload')
        _if = self.detect_if()
        local_ip = self.detect_ip(_if)
        idx = self.gateloads.index(local_ip)

        config_fname = 'templates/gateload_config_{}.json'.format(idx)
        put(config_fname, '/root/gateload_config.json')
        put('scripts/sgw_check_logs.sh', '/root/sgw_check_logs.sh')
        run('chmod 777 /root/sgw_*.sh')
        run('ulimit -n 65536; nohup /opt/gocode/bin/gateload '
            '-workload /root/gateload_config.json &>/root/gateload.log&',
            pty=False)

    @all_gateloads
    def collect_info_gateload(self):
        _if = self.detect_if()
        local_ip = self.detect_ip(_if)
        idx = self.gateloads.index(local_ip)

        logger.info('Collecting diagnostic information from gateload_{} {}'.format(idx, local_ip))
        run('rm -f gateload.log.gz', warn_only=True)
        run('gzip gateload.log', warn_only=True)
        put('scripts/sgw_check_logs.sh', '/root/sgw_check_logs.sh')
        run('chmod 777 /root/sgw_*.sh')
        run('/root/sgw_check_logs.sh gateload > sgw_check_logs.out', warn_only=True)
        self.try_get('gateload.log.gz', 'gateload.log-{}.gz'.format(idx))
        self.try_get('gateload_config.json', 'gateload_config_{}.json'.format(idx))
        self.try_get('gateload_expvars.json', 'gateload_expvar_{}.json'.format(idx))
        self.try_get('sgw_check_logs.out', 'sgw_check_logs_gateload_{}.out'.format(idx))

    @all_gateways
    def collect_profile_data_gateways(self):
        """
        Collect CPU and heap profile raw data as well as rendered pdfs
        from go tool pprof
        """
        _if = self.detect_if()
        local_ip = self.detect_ip(_if)
        idx = self.gateways.index(local_ip)

        logger.info('Collecting profiling data from gateway_{} {}'.format(idx, local_ip))

        put('scripts/sgw_collect_profile.sh', '/root/sgw_collect_profile.sh')
        run('chmod 777 /root/sgw_collect_profile.sh')
        run('/root/sgw_collect_profile.sh /opt/couchbase-sync-gateway/bin/sync_gateway /root', pty=False)
        self.try_get('profile_data.tar.gz', 'profile_data.tar-{}.gz'.format(idx))

    @all_hosts
    def clean_mongodb(self):
        for path in self.cluster_spec.paths:
            run('rm -fr {}/*'.format(path))
        run('rm -fr {}'.format(self.MONGO_DIR))

    @all_hosts
    def install_mongodb(self, url):
        self.wget(url, outdir='/tmp')
        archive = url.split('/')[-1]

        logger.info('Installing MongoDB')

        run('mkdir {}'.format(self.MONGO_DIR))
        run('tar xzf {} -C {} --strip-components 1'.format(archive,
                                                           self.MONGO_DIR))
        run('numactl --interleave=all {}/bin/mongod '
            '--dbpath={} --fork --logpath /tmp/mongodb.log'
            .format(self.MONGO_DIR, self.cluster_spec.paths[0]))

    def try_get(self, remote_path, local_path=None):
        try:
            get(remote_path, local_path)
        except:
            logger.warn("Exception calling get({}, {}).  Ignoring.".format(remote_path, local_path))

    @single_host
    def install_beer_samples(self):
        logger.info('run install_beer_samples')
        cmd = '/opt/couchbase/bin/cbdocloader  -n localhost:8091 -u Administrator -p password -b beer-sample /opt/couchbase/samples/beer-sample.zip'
        result = run(cmd, pty=False)
        return result

    @single_client
    def ycsb_load_run(self, path, cmd, log_path=None):
        if log_path:
            tmpcmd = 'rm -rf ' + log_path
            run(tmpcmd)
            tmpcmd = 'mkdir -p ' + log_path
            run(tmpcmd)
        load_run_cmd = 'cd {}'.format(path) + ' &&  mvn -pl com.yahoo.ycsb:couchbase2-binding -am ' \
                                              'clean package -Dmaven.test.skip -Dcheckstyle.skip=true && {}'.format(cmd)
        logger.info(" running command {}".format(load_run_cmd))
        return run(load_run_cmd)


class RemoteWindowsHelper(RemoteLinuxHelper):

    CB_DIR = '/cygdrive/c/Program\ Files/Couchbase/Server'

    VERSION_FILE = '/cygdrive/c/Program Files/Couchbase/Server/VERSION.txt'

    MAX_RETRIES = 5

    TIMEOUT = 600

    SLEEP_TIME = 60  # crutch

    PROCESSES = ('erl*', 'epmd*')

    @staticmethod
    def exists(fname):
        r = run('test -f "{}"'.format(fname), warn_only=True, quiet=True)
        return not r.return_code

    @single_host
    def detect_pkg(self):
        logger.info('Detecting package manager')
        return 'exe'

    @single_host
    def detect_openssl(self, pkg):
        pass

    def reset_swap(self):
        pass

    def drop_caches(self):
        pass

    def set_swappiness(self):
        pass

    def disable_thp(self):
        pass

    def detect_ip(self):
        return run('ipconfig | findstr IPv4').split(': ')[1]

    @all_hosts
    def collect_info(self):
        logger.info('Running cbcollect_info')

        run('rm -f *.zip')

        fname = '{}.zip'.format(uhex())
        r = run('{}/bin/cbcollect_info.exe {}'.format(self.CB_DIR, fname),
                warn_only=True)
        if not r.return_code:
            get('{}'.format(fname))
            run('rm -f {}'.format(fname))

    @all_hosts
    def clean_data(self):
        for path in self.cluster_spec.paths:
            path = path.replace(':', '').replace('\\', '/')
            path = '/cygdrive/{}'.format(path)
            run('rm -fr {}/*'.format(path))

    @all_hosts
    def kill_processes(self):
        logger.info('Killing {}'.format(', '.join(self.PROCESSES)))
        run('taskkill /F /T /IM {}'.format(' /IM '.join(self.PROCESSES)),
            warn_only=True, quiet=True)

    def kill_installer(self):
        run('taskkill /F /T /IM setup.exe', warn_only=True, quiet=True)

    def clean_installation(self):
        with settings(warn_only=True):
            run('rm -fr {}'.format(self.CB_DIR))

    @all_hosts
    def uninstall_couchbase(self, pkg):
        local_ip = self.detect_ip()
        logger.info('Uninstalling Package on {}'.format(local_ip))

        if self.exists(self.VERSION_FILE):
            for retry in range(self.MAX_RETRIES):
                self.kill_installer()
                try:
                    r = run('./setup.exe -s -f1"C:\\uninstall.iss"',
                            warn_only=True, quiet=True, timeout=self.TIMEOUT)
                    if not r.return_code:
                        t0 = time.time()
                        while self.exists(self.VERSION_FILE) and \
                                time.time() - t0 < self.TIMEOUT:
                            logger.info('Waiting for Uninstaller to finish on {}'.format(local_ip))
                            time.sleep(5)
                        break
                    else:
                        logger.warn('Uninstall script failed to run on {}'.format(local_ip))
                except CommandTimeout:
                    logger.warn("Uninstall command timed out - retrying on {} ({} of {})"
                                .format(local_ip, retry, self.MAX_RETRIES))
                    continue
            else:
                logger.warn('Uninstaller failed with no more retries on {}'
                            .format(local_ip))
        else:
            logger.info('Package not present on {}'.format(local_ip))

        logger.info('Cleaning registry on {}'.format(local_ip))
        self.clean_installation()

    @staticmethod
    def put_iss_files(version):
        logger.info('Copying {} ISS files'.format(version))
        put('scripts/install_{}.iss'.format(version),
            '/cygdrive/c/install.iss')
        put('scripts/uninstall_{}.iss'.format(version),
            '/cygdrive/c/uninstall.iss')

    @all_hosts
    def install_couchbase(self, pkg, url, filename, version=None):
        self.kill_installer()
        run('rm -fr setup.exe')
        self.wget(url, outfile='setup.exe')
        run('chmod +x setup.exe')

        self.put_iss_files(version)

        local_ip = self.detect_ip()

        logger.info('Installing Package on {}'.format(local_ip))
        try:
            run('./setup.exe -s -f1"C:\\install.iss"')
        except:
            logger.error('Install script failed on {}'.format(local_ip))
            raise

        while not self.exists(self.VERSION_FILE):
            logger.info('Waiting for Installer to finish on {}'.format(local_ip))
            time.sleep(5)

        logger.info('Sleeping for {} seconds'.format(self.SLEEP_TIME))
        time.sleep(self.SLEEP_TIME)

    def restart(self):
        pass

    def restart_with_alternative_num_vbuckets(self, num_vbuckets):
        pass

    def disable_wan(self):
        pass

    def enable_wan(self):
        pass

    def filter_wan(self, *args):
        pass

    def tune_log_rotation(self):
        pass

    def build_secondary_index(self, index_nodes, bucket, indexes, fields,
                              secondarydb, where_map):
        super(RemoteWindowsHelper, self).build_secondary_index(
            index_nodes, bucket, indexes, fields, secondarydb, where_map,
            commandPath='/cygdrive/c/program\\ files/Couchbase/Server/bin/')

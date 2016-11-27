import contextlib
import psycopg2
import datetime
import fileinput
import os
import re
import glob
import logging
import shutil
import subprocess
import sys
import operator
import time
import dateutil.parser
from dateutil.relativedelta import relativedelta
import werkzeug

import openerp
from openerp.osv import orm, fields
from openerp.addons.runbot.runbot import mkdirs, uniq_list, now, grep, locked, fqdn, rfind, _re_error, _re_warning, RunbotController, decode_utf, run, dt2time
from openerp.tools import config, appdirs
from openerp import http, SUPERUSER_ID, api
from openerp.http import request

_logger = logging.getLogger(__name__)

BRANCH_REGEXP = r'^([0-9]+\.[0-9]+)(-|$)'

MAGIC_PID_RUN_NEXT_JOB = -2

#http://stackoverflow.com/questions/39086/search-and-replace-a-line-in-a-file-in-python
def replace(filename, searchExp, replaceExp):
    _logger.debug('replace(%s)', [filename, searchExp, replaceExp])
    try:
        for line in fileinput.input(filename, inplace=1):
            if searchExp in line:
                line = line.replace(searchExp, replaceExp)
            sys.stdout.write(line)
    except OSError:
        _logger.warning('Cannot replace in file %s', filename, exc_info=True)


def exec_pg_environ():
    """
    Force the database PostgreSQL environment variables to the database
    configuration of Odoo.

    Note: On systems where pg_restore/pg_dump require an explicit password
    (i.e.  on Windows where TCP sockets are used), it is necessary to pass the
    postgres user password in the PGPASSWORD environment variable or in a
    special .pgpass file.

    See also http://www.postgresql.org/docs/8.4/static/libpq-envars.html
    """
    env = os.environ.copy()
    db_user = os.getenv('DB_ENV_POSTGRES_USER') or os.getenv('RDS_USERNAME')
    if db_user:
        env['PGUSER'] = db_user
    db_host = os.getenv('DB_PORT_5432_TCP_ADDR') or os.getenv('RDS_HOSTNAME')
    if db_host:
        env['PGHOST'] = db_host
    db_port = os.getenv('DB_PORT_5432_TCP_PORT') or os.getenv('RDS_PORT')
    if db_port:
        env['PGPORT'] = db_port

    db_password = os.getenv('DB_ENV_POSTGRES_PASSWORD') or os.getenv('RDS_PASSWORD')
    if db_password:
        env['PGPASSWORD'] = db_password

    return env


@contextlib.contextmanager
def local_pgadmin_cursor():
    env = exec_pg_environ()
    cnx = None
    try:
        cnx = psycopg2.connect(database="postgres",
                               user=env.get('PGUSER'),
                               password=env.get('PGPASSWORD'),
                               host=env.get('PGHOST'),
                               port=env.get('PGPORT'),
                               )
        cnx.autocommit = True  # required for admin commands
        yield cnx.cursor()
    finally:
        if cnx: cnx.close()


class runbot_repo(orm.Model):
    _inherit = "runbot.repo"

    def _get_base(self, cr, uid, ids, field_name, arg, context=None):
        result = super(runbot_repo, self)._get_base(cr, uid, ids, field_name, arg, context)
        for id in result:
            result[id] = result[id].replace('https///', '')
        return result

    _columns = {
        'nickname': fields.char('Nickname', help='Name to be used in fast_launch'),
        'base': fields.function(_get_base, type='char', string='Base URL', readonly=1),
        'is_addons_dev': fields.boolean('addons-dev'),
        'is_saas': fields.boolean('odoo-saas-tools'),
        'run_tests': fields.boolean('Run tests', default=True),
        'server_wide_modules': fields.char('server wide modules'),
    }

    def git_export_file(self, cr, uid, ids, treeish, dest, filename, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            _logger.debug('checkout file %s %s %s', repo.name, treeish, dest)
            p1 = subprocess.Popen(['git', '--git-dir=%s' % repo.path, 'archive', treeish, filename], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(['tar', '-xC', dest], stdin=p1.stdout, stdout=subprocess.PIPE)
            p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
            p2.communicate()[0]

    def github(self, cr, uid, ids, url, payload=None, ignore_errors=False, context=None):
        _logger.debug('sleep before request')
        time.sleep(1)
        return super(runbot_repo, self).github(cr, uid, ids, url, payload=payload, ignore_errors=ignore_errors, context=context)


    def cron_fetch_odoo(self, cr, uid, ids=None, context=None):
        # make git fetch for odoo and other foreign repos
        ids = self.search(cr, uid, [('mode', '=', 'disabled')], context=context)
        self.update(cr, uid, ids, context=context)

    def update(self, cr, uid, ids, context=None):
        for repo in self.browse(cr, uid, ids, context=context):
            try:
                self.update_git(cr, uid, repo)
            except subprocess.CalledProcessError:
                _logger.exception('Ignored git error while updating repo %s', repo.name)

    def update_git(self, cr, uid, repo, context=None):
        _logger.debug('repo %s updating branches', repo.name)

        Build = self.pool['runbot.build']
        Branch = self.pool['runbot.branch']

        if not os.path.isdir(os.path.join(repo.path)):
            os.makedirs(repo.path)
        if not os.path.isdir(os.path.join(repo.path, 'refs')):
            run(['git', 'clone', '--bare', repo.name, repo.path])

        # check for mode == hook
        fname_fetch_head = os.path.join(repo.path, 'FETCH_HEAD')
        if os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if repo.mode == 'hook' and repo.hook_time and dt2time(repo.hook_time) < fetch_time:
                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago hook_time: %ss ago',
                              repo.name, int(t0 - fetch_time), int(t0 - dt2time(repo.hook_time)))
                return

        repo.git(['gc', '--auto', '--prune=all'])
        repo.git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*'])
        repo.git(['fetch', '-p', 'origin', '+refs/pull/*/head:refs/pull/*'])

        fields = ['refname','objectname','committerdate:iso8601','authorname','authoremail','subject','committername','committeremail']
        fmt = "%00".join(["%("+field+")" for field in fields])
        git_refs = repo.git(['for-each-ref', '--format', fmt, '--sort=-committerdate', 'refs/heads', 'refs/pull'])
        git_refs = git_refs.strip()

        refs = [[decode_utf(field) for field in line.split('\x00')] for line in git_refs.split('\n')]

        i = 0
        for name, sha, date, author, author_email, subject, committer, committer_email in refs:
            # create or get branch
            branch_ids = Branch.search(cr, uid, [('repo_id', '=', repo.id), ('name', '=', name)])
            if branch_ids:
                branch_id = branch_ids[0]
            else:
                _logger.debug('repo %s found new branch %s', repo.name, name)
                if repo.mode == 'disabled' and name.startswith('refs/pull/'):
                    _logger.debug('skip pull %s for disabled repo %s', name, repo.name)
                    continue
                branch_id = Branch.create(cr, uid, {'repo_id': repo.id, 'name': name})
                i += 1

            branch = Branch.browse(cr, uid, [branch_id], context=context)[0]
            # skip build for old branches
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(30) < datetime.datetime.now():
                continue
            # create build (and mark previous builds as skipped) if not found
            build_ids = Build.search(cr, uid, [('branch_id', '=', branch.id), ('name', '=', sha)])
            if not build_ids:
                _logger.debug('repo %s branch %s new build found revno %s', branch.repo_id.name, branch.name, sha)
                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': dateutil.parser.parse(date[:19]),
                }

                if not branch.sticky:
                    skipped_build_sequences = Build.search_read(cr, uid, [('branch_id', '=', branch.id), ('state', '=', 'pending')],
                                                                fields=['sequence'], order='sequence asc', context=context)
                    if skipped_build_sequences:
                        to_be_skipped_ids = [build['id'] for build in skipped_build_sequences]
                        Build.skip(cr, uid, to_be_skipped_ids, context=context)
                        # new order keeps lowest skipped sequence
                        build_info['sequence'] = skipped_build_sequences[0]['sequence']
                Build.create(cr, uid, build_info)
            if i == 10:
                _logger.debug('Force commit new branches')
                i = 0
                cr.commit()

        # skip old builds (if their sequence number is too low, they will not ever be built)
        skippable_domain = [('repo_id', '=', repo.id), ('state', '=', 'pending')]
        icp = self.pool['ir.config_parameter']
        running_max = int(icp.get_param(cr, uid, 'runbot.running_max', default=75))
        to_be_skipped_ids = Build.search(cr, uid, skippable_domain, order='sequence desc', offset=running_max)
        Build.skip(cr, uid, to_be_skipped_ids)


class runbot_branch(orm.Model):
    _inherit = "runbot.branch"

    def _get_pull_files(self, cr, uid, ids, context=None):
        assert len(ids) == 1
        branch = self.browse(cr, uid, ids[0], context=context)
        repo = branch.repo_id
        if repo.token and branch.name.startswith('refs/pull/'):
            pull_number = branch.name[len('refs/pull/'):]
            return repo.github('/repos/:owner/:repo/pulls/%s/files' % pull_number, ignore_errors=True) or []
        return []

    def get_updated_modules(self, cr, uid, ids, context=None):
        assert len(ids) == 1, 'get_updated_modules must called for single record only'
        for bid in ids:
            files = self._get_pull_files(cr, uid, [bid], context=context)
            if files:
                files = [f['raw_url'] for f in files]
                files = [f.split('/') for f in files]
                updated_modules = set([f[7] for f in files if len(f) > 8])
                return ','.join(updated_modules)

    def _get_pull_base_name(self, cr, uid, ids, field_name, arg, context=None):
        r = dict.fromkeys(ids, False)
        for bid in ids:
            pi = self._get_pull_info(cr, uid, [bid], context=context)
            if pi:
                r[bid] = pi['base']['ref']
        return r

    _columns = {
        #'pull_base_name': fields.function(_get_pull_base_name, type='char', string='PR Base name', readonly=1, store=True),
        #'updated_modules': fields.function(_get_updated_modules, type='char', string='Updated modules', help='Comma-separated list of updated modules (for PR)', readonly=1, store=False),
    }

def fix_long_line(s):
    return ', '.join(s.split(','))

class runbot_build(orm.Model):
    _inherit = "runbot.build"

    def _local_pg_dropdb(self, cr, uid, dbname):
        openerp.service.db._drop_conn(cr, dbname)
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute('DROP DATABASE IF EXISTS "%s"' % dbname)
        # cleanup filestore
        datadir = appdirs.user_data_dir()
        paths = [os.path.join(datadir, pn, 'filestore', dbname) for pn in 'OpenERP Odoo'.split()]
        run(['rm', '-rf'] + paths)

    def _local_pg_createdb(self, cr, uid, dbname):
        self._local_pg_dropdb(cr, uid, dbname)
        _logger.debug("createdb %s", dbname)
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("""CREATE DATABASE "%s" TEMPLATE template0 LC_COLLATE 'C' ENCODING 'unicode'""" % dbname)

    def job_01_check_branch_name(self, cr, uid, build, lock_path, log_path):
        branch = build.branch_id
        pi = branch._get_pull_info()
        if not pi:
            build._log('check_branch_name', 'Enable to get pull info')

            return MAGIC_PID_RUN_NEXT_JOB

        # PR branch name, e.g. 	8.0-mail_private
        head = branch.pull_head_name
        # Target branch name, e.g. 8.0
        base = pi['base']['ref']

        success = True
        description = "Branch names are correct"
        if head.startswith(base):
            pass
        else:
            m = re.search(BRANCH_REGEXP, base)
            if not m:
                # base has wrong format
                build._log('check_branch_name', "Wrong branch name: {head}. It has to be started with {base} (e.g. {base}, {base}-feature)".format(base=base, head=head))
                description = "Wrong branch name: %s" % head
                success = False

            if success:
                m = re.search(BRANCH_REGEXP, head)
                base_version = re.search(BRANCH_REGEXP, base).group(1)
                if not m:
                    # head has wrong format
                    build._log('check_branch_name', "Wrong branch name format: {head}. It has to be following format: {BRANCH_REGEXP} (e.g. {base_version}, {base_version}-feature)".format(base_version=base_version, head=head, BRANCH_REGEXP=BRANCH_REGEXP))
                    description = "Wrong branch name format: %s" % head
                    success = False

            if success:
                # correct formats
                base_version = re.search(BRANCH_REGEXP, base).group(1)
                head_version = re.search(BRANCH_REGEXP, head).group(1)
                if head_version != base_version:
                    build._log('check_branch_name', "Head and Base have different prefixes: %s and %s. Probably, pull request is made to a wrong branch" % (head_version, base_version))
                    description = "Branch names are mismatched: %s != %s" % (base_version, head_version)
                    success = False

        runbot_domain = self.pool['runbot.repo'].domain(cr, uid)
        state = 'success' if success else 'failure'
        status = {
            "state": state,
            "target_url": "http://%s/runbot/build/%s" % (runbot_domain, build.id),
            "description": description,
            "context": "ci/branches"
        }
        build._log('check_branch_name', '%s' % state)
        build.repo_id.github('/repos/:owner/:repo/statuses/%s' % build.name, status, ignore_errors=True)

        return MAGIC_PID_RUN_NEXT_JOB

    def _get_domain(self, cr, uid, ids, field_name, arg, context=None):
        result = {}
        domain = self.pool['runbot.repo'].domain(cr, uid)
        for build in self.browse(cr, uid, ids, context=context):
            if build.repo_id.nginx:
                db_type = 'base' if build.repo_id.modules_auto == 'none' else 'all'
                if build.repo_id.is_saas:
                    result[build.id] = "%s--%s---portal.%s" % (build.dest, db_type, build.host)
                else:
                    result[build.id] = "%s-%s.%s" % (build.dest, db_type, build.host)
            else:
                result[build.id] = "%s:%s" % (domain, build.port)
        return result

    _columns = {
        'auto_modules': fields.char("Filtered modules to test in *-all* installation"),
        'unsafe_modules': fields.char("Unsafe modules to be checkouted"),
        'domain': fields.function(_get_domain, type='char', string='URL'),
    }


    def _install_and_test(self, cr, uid, build, lock_path, log_path, dbname, modules):
        build._log('_install_and_test', 'DB: %s, Modules: %s' % (dbname, fix_long_line(modules)))
        self._local_pg_createdb(cr, uid, dbname)
        cmd, mods = build.cmd()
        if build.repo_id.run_tests and grep(build.server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['--db-filter', '.*']
        cmd += ['-d', dbname, '-i', modules, '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=4200)

    def _install_and_test_saas(self, cr, uid, build, lock_path, log_path, suffix, modules):
        cmd = build.cmd_saas()
        cmd += ['--portal-create',
                '--server-create',
                '--plan-create',
                '--suffix', suffix,
                '--install-modules', modules,
                '--test',
        ]
        if build.repo_id.run_tests:
            cmd.append("--odoo-test-enable")
        build._log('_install_and_test_saas', 'run saas.py: %s' % fix_long_line(' '.join(cmd)))
        build.write({'job_start': now()})
        return self.spawn(cmd, lock_path, log_path, cpu_limit=4200)

    def job_10_test_base(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.is_saas:
            build._log('test_base', 'Test updated saas modules')
            return self._install_and_test_saas(cr, uid, build, lock_path, log_path, '%s--base' % build.dest, build.modules)

        build._log('test_base', 'Test Updated and explicit modules')
        return self._install_and_test(cr, uid, build, lock_path, log_path, "%s-base" % build.dest, build.modules)

    def job_20_test_all(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.modules_auto == 'none':
            build._log('test_all', 'Testing all modules is not configured for this repo')
            return MAGIC_PID_RUN_NEXT_JOB

        if build.repo_id.is_saas:
            build._log('test_all', '=============  test all SAAS modules =============')
            return self._install_and_test_saas(cr, uid, build, lock_path, log_path, '%s--all' % build.dest, build.auto_modules)

        build._log('test_all', '============= Test all modules =============')
        return self._install_and_test(cr, uid, build, lock_path, log_path, "%s-all" % build.dest, build.auto_modules)

    @api.multi
    def get_modules_to_check_pylint(self):
        self.ensure_one()
        if self.auto_modules:
            return self.auto_modules.split(',')
        else:
            return False

    def job_30_run(self, cr, uid, build, lock_path, log_path):
        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        if build.repo_id.modules_auto == 'none':
            log_all = build.path('logs', 'job_10_test_base.txt')
            log_time = time.localtime(os.path.getmtime(log_all))
        else:
            log_all = build.path('logs', 'job_20_test_all.txt')
            log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time),
        }
        if grep(log_all, ".modules.loading: Modules loaded."):
            if rfind(log_all, _re_error):
                v['result'] = "ko"
            elif rfind(log_all, _re_warning):
                v['result'] = "warn"
            elif not grep(build.server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                v['result'] = "ok"
        else:
            v['result'] = "ko"

        if build.branch_id.repo_id.is_saas:
            if grep(log_all, "SaaS tests were passed successfully"):
                build._log("job_30_run", "SaaS tests were passed successfully")
            else:
                v['result'] = "ko"
                build._log("job_30_run", "SAAS ERROR. Something wrong with saas.py run. Check logs.")

        build.write(v)
        build.github_status()
        self.check_pylint_result(cr, uid, build)

        # run server
        cmd, mods = build.cmd()
        if os.path.exists(build.server('addons/im_livechat')):
            if build.branch_id.repo_id.is_saas:
                cmd += ["--workers", "3"]
            else:
                cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        # comment out origin code. We override exp_list function to allow cron use only databases, which starts with build.dest
        #cmd += ['-d', "%s-all" % build.dest]

        if grep(build.server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter','%d.*$']
            else:
                cmd += ['--db-filter','%s.*$' % build.dest]

        ## Web60
        #self.client_web_path=os.path.join(self.running_path,"client-web")
        #self.client_web_bin_path=os.path.join(self.client_web_path,"openerp-web.py")
        #self.client_web_doc_path=os.path.join(self.client_web_path,"doc")
        #webclient_config % (self.client_web_port+port,self.server_net_port+port,self.server_net_port+port)
        #cfgs = [os.path.join(self.client_web_path,"doc","openerp-web.cfg"), os.path.join(self.client_web_path,"openerp-web.cfg")]
        #for i in cfgs:
        #    f=open(i,"w")
        #    f.write(config)
        #    f.close()
        #cmd=[self.client_web_bin_path]

        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def checkout_update_odoo(self, build):
        # update default smtp to new value (see https://github.com/it-projects-llc/odoo-runbot-docker )
        replace(build.server('addons', 'base', 'base_data.xml'), '>localhost<', '>postfix<')

        # increase timeout for phantom_js
        replace(build.server('tests', 'common.py'), 'timeout=60', 'timeout=120')

        # increase timeout in _wait_remaining_requests, because it could dump too much logs
        replace(build.server('tests', 'common.py'), 'thread.join(0.05)', 'thread.join(5)')
        replace(build.server('tests', 'common.py'), 'thread.sleep(0.05)', 'thread.sleep(5)')

        # always close cron db connnections
        replace(build.server('service', 'server.py'), 'if len(db_names) > 1:', 'if True:')


        # help cron workers in 8.0 to select only necessary databases
        replace(build.server('service', 'db.py'), 'def exp_list(',
                '''def exp_list(*args, **kwargs):
    res = exp_list_origin(*args, **kwargs)
    return [db for db in res if db.startswith('%s-')]

def exp_list_origin(''' % build.dest)

        # help cron workers in 9.0 to select only necessary databases
        replace(build.server('service', 'db.py'), 'def list_dbs(',
                '''def list_dbs(*args, **kwargs):
    res = list_dbs_origin(*args, **kwargs)
    return [db for db in res if db.startswith('%s-')]

def list_dbs_origin(''' % build.dest)

        # restriction for name of new databases
        replace(build.server('service', 'db.py'), 'def exp_create_database(',
                '''def exp_create_database(*args, **kwargs):
    db_name = args[0]
    if not db_name.startswith('%s-'):
        raise Exception("On runbot, you can create only database that starts with '%s-'")
    return exp_create_database_origin(*args, **kwargs)

def exp_create_database_origin(''' % (build.dest, build.dest))

        # restriction for name of duplicated databases
        replace(build.server('service', 'db.py'), 'def exp_duplicate_database(',
                '''def exp_duplicate_database(*args, **kwargs):
    db_name = args[1]
    if not db_name.startswith('%s-'):
        raise Exception("On runbot, you can create only database that starts with '%s-'")
    return exp_duplicate_database_origin(*args, **kwargs)

def exp_duplicate_database_origin(''' % (build.dest, build.dest))

        # restriction for new name on renaming  databases
        replace(build.server('service', 'db.py'), 'def exp_rename(',
                '''def exp_rename(*args, **kwargs):
    db_name = args[1]
    if not db_name.startswith('%s-'):
        raise Exception("On runbot, you can use only database name that starts with '%s-'")
    return exp_rename_origin(*args, **kwargs)

def exp_rename_origin(''' % (build.dest, build.dest))

    def checkout(self, cr, uid, ids, context=None):
        context = context or {}
        for build in self.browse(cr, uid, ids, context=context):
            # starts from scratch
            if os.path.isdir(build.path()):
                shutil.rmtree(build.path())

            # runbot log path
            mkdirs([build.path("logs"), build.server('addons')])

            repo_id, closest_name, server_match = build._get_closest_branch_name(build.branch_id.repo_id.id)
            # checkout branch
            build.branch_id.repo_id.git_export(build.name, build.path())
            if build.branch_id.repo_id.is_saas:
                # checkout saas.py from base for security reasons
                build.branch_id.repo_id.git_export_file(closest_name, build.path(), 'saas.py')

            # v6 rename bin -> openerp
            if os.path.isdir(build.path('bin/addons')):
                shutil.move(build.path('bin'), build.server())

            has_server = os.path.isfile(build.server('__init__.py'))
            server_match = 'builtin'

            # build complete set of modules to install
            modules_to_move = []
            modules_to_test = ((build.branch_id.modules or '') + ',' +
                               (build.repo_id.modules or ''))
            modules_to_test = filter(None, modules_to_test.split(','))
            auto_modules = [m for m in modules_to_test]
            explicit_modules = set(modules_to_test)
            _logger.debug("manual modules_to_test for build %s: %s", build.dest, modules_to_test)

            if not has_server:
                repo_modules = [
                    os.path.basename(os.path.dirname(a))
                    for a in (glob.glob(build.path('*/__openerp__.py')) +
                              glob.glob(build.path('*/__manifest__.py')))
                ]
                _logger.debug("repo modules for build %s: %s", build.dest, repo_modules)

                unsafe_modules = filter(None, (build.repo_id.server_wide_modules or '').split(','))
                unsafe_modules = [m for m in unsafe_modules if m in repo_modules]
                if unsafe_modules:
                    for m in unsafe_modules:
                        build.branch_id.repo_id.git_export_file(closest_name, build.path(), m)
                    build._log('checkout', 'Following modules are checkouted to base version for security reasons: %s' % unsafe_modules)



                if build.repo_id.modules_auto == 'repo':
                    auto_modules += repo_modules

                for extra_repo in build.repo_id.dependency_ids:
                    if build.repo_id.is_addons_dev:
                        # addons-yelizariev-9.0-some-feature -> addons-yelizariev
                        repo_name = None
                        try:
                            repo_name = re.match('([^0-9]*)-', build.branch_id.pull_head_name).group(1)

                        except:
                            pass

                        if repo_name and extra_repo.name.endswith('%s.git' % repo_name):
                            build._log('checkout', 'ignore repo "%s" as all modules are already in addons-dev branch' % repo_name)
                            continue
                    repo_id, closest_name, server_match = build._get_closest_branch_name(extra_repo.id)
                    extra_repo_name = extra_repo.name
                    try:
                        extra_repo_name = '/'.join(extra_repo_name.split('.')[-2].split('/')[-2:])
                    except:
                        pass
                    build._log('checkout', 'closest branch for %s is %s' % (extra_repo_name, closest_name))
                    repo = self.pool['runbot.repo'].browse(cr, uid, repo_id, context=context)
                    repo.git_export(closest_name, build.path())

                # Finally mark all addons to move to openerp/addons
                modules_to_move += [
                    os.path.dirname(module)
                    for module in (glob.glob(build.path('*/__openerp__.py')) +
                                   glob.glob(build.path('*/__manifest__.py')))
                ]

            # move all addons to server addons path
            for module in uniq_list(glob.glob(build.path('addons/*')) + modules_to_move):
                basename = os.path.basename(module)
                if os.path.exists(build.server('addons', basename)):
                    build._log(
                        'Building environment',
                        'You have duplicate modules in your branches "%s"' % basename
                    )
                    shutil.rmtree(build.server('addons', basename))
                shutil.move(module, build.server('addons'))
            self.checkout_update_odoo(build)
            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in (glob.glob(build.server('addons/*/__openerp__.py')) +
                          glob.glob(build.server('addons/*/__manifest__.py')))
            ]
            if build.repo_id.modules_auto == 'all' or (build.repo_id.modules_auto != 'none' and has_server):
                auto_modules += available_modules

            updated_modules = build.branch_id.get_updated_modules()
            _logger.debug('updated_modules %s', updated_modules)
            if updated_modules:
                modules_to_test += updated_modules.split(',')

            modules_to_test = self.filter_modules(cr, uid, modules_to_test,
                                                  set(available_modules), explicit_modules)
            auto_modules = self.filter_modules(cr, uid, auto_modules,
                                                  set(available_modules), explicit_modules)
            _logger.debug("modules_to_test for build %s: %s", build.dest, modules_to_test)
            _logger.debug("auto_modules for build %s: %s", build.dest, auto_modules)
            build._log('checkout', 'modules to install: %s' % modules_to_test)
            build.write({'server_match': server_match,
                         'unsafe_modules': ','.join(unsafe_modules),
                         'auto_modules': ','.join(auto_modules),
                         'modules': ','.join(modules_to_test)})

    def _get_closest_branch_name(self, cr, uid, ids, target_repo_id, context=None):
        """Return (repo, branch name) of the closest common branch between build's branch and
           any branch of target_repo or its duplicated repos.

        Rules priority for choosing the branch from the other repo is:
        1. Same branch name
        2. A PR whose head name match
        3. Match a branch which is the dashed-prefix of current branch name
        4. Common ancestors (git merge-base)
        Note that PR numbers are replaced by the branch name of the PR target
        to prevent the above rules to mistakenly link PR of different repos together.
        """
        assert len(ids) == 1
        branch_pool = self.pool['runbot.branch']

        build = self.browse(cr, uid, ids[0], context=context)
        branch, repo = build.branch_id, build.repo_id
        pi = context and context.get('pull_info')
        if not pi:
            pi = branch._get_pull_info()
            if context:
                context['pull_info'] = pi

        name = pi['base']['ref'] if pi else branch.branch_name
        if build.repo_id.is_addons_dev:
            m = re.search('-([0-9]+\.[0-9]+)-', name)
            if m:
                name = m.group(1)

        target_repo = self.pool['runbot.repo'].browse(cr, uid, target_repo_id, context=context)

        target_repo_ids = [target_repo.id]
        r = target_repo.duplicate_id
        while r:
            if r.id in target_repo_ids:
                break
            target_repo_ids.append(r.id)
            r = r.duplicate_id

        _logger.debug('Search closest of %s (%s) in repos %r', name, repo.name, target_repo_ids)

        sort_by_repo = lambda d: (not d['sticky'],      # sticky first
                                  target_repo_ids.index(d['repo_id'][0]),
                                  -1 * len(d.get('branch_name', '')),
                                  -1 * d['id'])
        result_for = lambda d, match='exact': (d['repo_id'][0], d['name'], match)
        branch_exists = lambda d: branch_pool._is_on_remote(cr, uid, [d['id']], context=context)
        fields = ['name', 'repo_id', 'sticky']

        # 1. same name, not a PR
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('branch_name', '=', name),
            ('name', '=like', 'refs/heads/%'),
        ]
        targets = branch_pool.search_read(cr, uid, domain, fields, order='id DESC',
                                          context=context)
        targets = sorted(targets, key=sort_by_repo)
        if targets and branch_exists(targets[0]):
            return result_for(targets[0])

        # 2. PR with head name equals
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('pull_head_name', '=', name),
            ('name', '=like', 'refs/pull/%'),
        ]
        pulls = branch_pool.search_read(cr, uid, domain, fields, order='id DESC',
                                        context=context)
        pulls = sorted(pulls, key=sort_by_repo)
        for pull in pulls:
            pi = branch_pool._get_pull_info(cr, uid, [pull['id']], context=context)
            if pi.get('state') == 'open':
                return result_for(pull)

        # 3. Match a branch which is the dashed-prefix of current branch name
        branches = branch_pool.search_read(
            cr, uid,
            [('repo_id', 'in', target_repo_ids), ('name', '=like', 'refs/heads/%')],
            fields + ['branch_name'], order='id DESC', context=context
        )
        branches = sorted(branches, key=sort_by_repo)

        for branch in branches:
            if name.startswith(branch['branch_name'] + '-') and branch_exists(branch):
                return result_for(branch, 'prefix')

        # 4. Common ancestors (git merge-base)
        for target_id in target_repo_ids:
            common_refs = {}
            cr.execute("""
                SELECT b.name
                  FROM runbot_branch b,
                       runbot_branch t
                 WHERE b.repo_id = %s
                   AND t.repo_id = %s
                   AND b.name = t.name
                   AND b.name LIKE 'refs/heads/%%'
            """, [repo.id, target_id])
            for common_name, in cr.fetchall():
                try:
                    commit = repo.git(['merge-base', branch['name'], common_name]).strip()
                    cmd = ['log', '-1', '--format=%cd', '--date=iso', commit]
                    common_refs[common_name] = repo.git(cmd).strip()
                except subprocess.CalledProcessError:
                    # If merge-base doesn't find any common ancestor, the command exits with a
                    # non-zero return code, resulting in subprocess.check_output raising this
                    # exception. We ignore this branch as there is no common ref between us.
                    continue
            if common_refs:
                b = sorted(common_refs.iteritems(), key=operator.itemgetter(1), reverse=True)[0][0]
                return target_id, b, 'fuzzy'

        # 5. last-resort value
        return target_repo_id, 'master', 'default'

    def cmd(self, cr, uid, ids, context=None):
        cmd, modules = super(runbot_build, self).cmd(cr, uid, ids, context=context)
        for build in self.browse(cr, uid, ids, context=context):
            # add addons path in order to ignore runbot config
            addons_path = ','.join([
                build.server('addons')
            ])
            cmd += ['--addons-path', addons_path]
            if build.repo_id.server_wide_modules:
                cmd += ['--load', build.repo_id.server_wide_modules]

        env = exec_pg_environ()
        cmd += ["--db_user", env.get('PGUSER'),
                "--db_password", env.get('PGPASSWORD'),
                "--db_host", env.get('PGHOST'),
                "--db_port", env.get('PGPORT'),
                ]

        if not modules:
            modules = 'base'

        return cmd, modules

    def cmd_saas(self, cr, uid, ids, context=None):
        cmd = []
        base_domain = self.pool.get('ir.config_parameter').get_param(cr, uid, 'runbot.domain', fqdn())
        for build in self.browse(cr, uid, ids, context=context):
            cmd, mods = build.cmd()
            server_path = cmd[1]
            cmd = ['python', build.path('saas.py'),
               '--odoo-script=%s' % server_path,
               '--odoo-xmlrpc-port=%s' % build.port,
               '--odoo-longpolling-port=%s' % (build.port + 1),
               '--local-xmlrpc-port=%s' % build.port,
               '--portal-db-name', '{suffix}---portal',
               '--server-db-name', '{suffix}---server',
               '--plan-template-db-name', '{suffix}---template',
               '--plan-clients', '{suffix}---client-%i',
               '--base-domain', base_domain,
               '--odoo-db-filter=%d',
            ]
            if grep(build.server("tools/config.py"), "data-dir"):
                datadir = build.path('datadir')
                cmd += ["--odoo-data-dir", datadir]
            if grep(build.server("tools/config.py"), "log-db"):
                logdb = cr.dbname
                if config['db_host'] and grep(build.server('sql_db.py'), 'allow_uri'):
                    logdb = 'postgres://{cfg[db_user]}:{cfg[db_password]}@{cfg[db_host]}/{db}'.format(cfg=config, db=cr.dbname)
                cmd += ["--odoo-log-db=%s" % logdb]
            addons_path = ','.join([
                build.server('addons')
            ])
            cmd += ['--odoo-addons-path', addons_path]

        return cmd

    def schedule(self, cr, uid, ids, context=None):
        jobs = self.list_jobs()

        icp = self.pool['ir.config_parameter']
        # For retro-compatibility, keep this parameter in seconds
        default_timeout = int(icp.get_param(cr, uid, 'runbot.timeout', default=1800)) / 60

        for build in self.browse(cr, uid, ids, context=context):
            if build.state == 'pending':
                # allocate port and schedule first job
                port = self.find_port(cr, uid)
                values = {
                    'host': fqdn(),
                    'port': port,
                    'state': 'testing',
                    'job': jobs[0],
                    'job_start': now(),
                    'job_end': False,
                }
                build.write(values)
                #cr.commit()
            else:
                # check if current job is finished
                lock_path = build.path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    timeout = (build.branch_id.job_timeout or default_timeout) * 60
                    if build.job != jobs[-1] and build.job_time > timeout:
                        build.logger('%s time exceded (%ss)', build.job, build.job_time)
                        build.write({'job_end': now()})
                        build.kill(result='killed')
                    continue
                build.logger('%s finished', build.job)
                # schedule
                v = {}
                # testing -> running
                if build.job == jobs[-2]:
                    v['state'] = 'running'
                    v['job'] = jobs[-1]
                    v['job_end'] = now(),
                # running -> done
                elif build.job == jobs[-1]:
                    v['state'] = 'done'
                    v['job'] = ''
                # testing
                else:
                    v['job'] = jobs[jobs.index(build.job) + 1]
                build.write(v)
            build.refresh()

            # run job
            pid = None
            if build.state != 'done':
                build.logger('running %s', build.job)
                job_method = getattr(self,build.job)
                mkdirs([build.path('logs')])
                lock_path = build.path('logs', '%s.lock' % build.job)
                log_path = build.path('logs', '%s.txt' % build.job)
                pid = job_method(cr, uid, build, lock_path, log_path)
                build.write({'pid': pid})
            # needed to prevent losing pids if multiple jobs are started and one them raise an exception
            cr.commit()

            if pid == -2:
                # no process to wait, directly call next job
                # FIXME find a better way that this recursive call
                build.schedule()

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build._local_cleanup()

    def _local_cleanup(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            # Cleanup the *local* cluster
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname LIKE %s
                """, [build.dest + '%'])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(cr, uid, db)

        # cleanup: find any build older than 7 days.
        root = self.pool['runbot.repo'].root(cr, uid)
        build_dir = os.path.join(root, 'build')
        builds = os.listdir(build_dir)
        cr.execute("""
            SELECT dest
              FROM runbot_build
             WHERE dest IN %s
               AND (state != 'done' OR job_end > (now() - interval '7 days'))
        """, [tuple(builds)])
        actives = set(b[0] for b in cr.fetchall())

        for b in builds:
            path = os.path.join(build_dir, b)
            if b not in actives and os.path.isdir(path):
                shutil.rmtree(path)

        # Clean heavy folders shortly after stopping
        # Basically, we need to keep logs only (for 7 days)
        root = self.pool['runbot.repo'].root(cr, uid)
        build_dir = os.path.join(root, 'build')
        builds = os.listdir(build_dir)
        cr.execute("""
            SELECT dest
              FROM runbot_build
             WHERE dest IN %s
               AND (state != 'done' OR job_end > (now() - interval '20 minutes'))
        """, [tuple(builds)])
        actives = set(b[0] for b in cr.fetchall())

        for b in builds:
            if b in actives:
                continue
            paths = [
                os.path.join(build_dir, b, 'datadir'),
                os.path.join(build_dir, b, 'doc'),
                os.path.join(build_dir, b, 'setup'),
                os.path.join(build_dir, b, 'openerp', 'addons'),
                os.path.join(build_dir, b, 'odoo', 'addons'),
            ]
            for path in paths:
                if os.path.isdir(path):
                    _logger.debug('rmtree %s', path)
                    shutil.rmtree(path)


class RunbotControllerCustom(RunbotController):

    def build_info(self, build):
        res = super(RunbotControllerCustom, self).build_info(build)
        types = ['base']
        if build.repo_id.modules_auto != 'none':
            types.append('all')
        for t in types:
            k = 'domain_%s' % t
            dest = build.dest
            if build.repo_id.is_saas:
                v = '%s--%s---portal' % (dest, t)
            else:
                v = '%s-%s' % (dest, t)
            res[k] = '%s.%s' % (v, build.host)
        return res

    @http.route(['/runbot/hook/<repo_id>'], type='http', auth="public", website=True)
    def hook(self, repo_id=None, **post):
        # TODO if repo_id == None parse the json['repository']['ssh_url'] and find the right repo
        res = request.registry['runbot.repo'].search(request.cr, SUPERUSER_ID, [('nickname', '=', repo_id)])
        if res:
            repo_id = res[0]
        else:
            repo_id = int(repo_id)
        repo = request.registry['runbot.repo'].browse(request.cr, SUPERUSER_ID, [repo_id])
        repo.hook_time = datetime.datetime.now().strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT)
        return ""

    @http.route([
        '/runbot/badge/<repo_id_or_nickname>/<branch>.svg',
        '/runbot/badge/<any(default,flat):theme>/<repo_id_or_nickname>/<branch>.svg',
    ], type="http", auth="public", methods=['GET', 'HEAD'])
    def badge(self, repo_id_or_nickname, branch, theme='default', nickname=None):
        repo_id = None
        nickname = None
        try:
            repo_id = int(repo_id_or_nickname)
        except:
            nickname = repo_id_or_nickname

        if nickname:
            repo_id = request.registry['runbot.repo'].search(
                request.cr, SUPERUSER_ID,
                [('nickname', '=', nickname)])
            if repo_id:
                repo_id = repo_id[0]

        return super(RunbotControllerCustom, self).badge(repo_id, branch, theme)

    @http.route(['/runbot/b/<branch_name>', '/runbot/<model("runbot.repo"):repo>/<branch_name>', '/demo/<nickname>/<branch_name>'], type='http', auth="public", website=True)
    def fast_launch(self, branch_name=False, repo=False, nickname=False, **post):
        pool, cr, uid, context = request.registry, request.cr, request.uid, request.context
        Build = pool['runbot.build']

        domain = [('branch_id.branch_name', '=', branch_name)]

        if repo:
            domain.extend([('branch_id.repo_id', '=', repo.id)])
            order="sequence desc"
        elif nickname:
            domain.extend([('branch_id.repo_id.nickname', '=', nickname)])
            order="sequence desc"
        else:
            order = 'repo_id ASC, sequence DESC'

        # Take the 10 lasts builds to find at least 1 running... Else no luck
        builds = Build.search(cr, uid, domain, order=order, limit=10, context=context)

        if builds:
            last_build = False
            for build in Build.browse(cr, uid, builds, context=context):
                if build.state == 'running' or (build.state == 'duplicate' and build.duplicate_id.state == 'running'):
                    last_build = build if build.state == 'running' else build.duplicate_id
                    break

            if not last_build:
                # Find the last build regardless the state to propose a rebuild
                last_build = Build.browse(cr, uid, builds[0], context=context)

            if last_build.state != 'running':
                url = "/runbot/build/%s?ask_rebuild=1" % last_build.id
            else:
                url = ("http://%s/web" % last_build.domain)
        else:
            return request.not_found()
        return werkzeug.utils.redirect(url)

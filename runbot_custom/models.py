import os
import re
import glob
import logging
import shutil
import subprocess
import operator
import time

import openerp
from openerp.osv import orm, fields
from openerp.addons.runbot.runbot import mkdirs, uniq_list, now, grep

_logger = logging.getLogger(__name__)

MAGIC_PID_RUN_NEXT_JOB = -2


class runbot_repo(orm.Model):
    _inherit = "runbot.repo"

    def _get_base(self, cr, uid, ids, field_name, arg, context=None):
        result = super(runbot_repo, self)._get_base(cr, uid, ids, field_name, arg, context)
        for id in result:
            result[id] = result[id].replace('https///', '')
        return result

    _columns = {
        'base': fields.function(_get_base, type='char', string='Base URL', readonly=1),
        'is_addons_dev': fields.boolean('addons-dev'),
        'install_updated_modules': fields.boolean('Install updated modules'),
    }

    def github(self, cr, uid, ids, url, payload=None, ignore_errors=False, context=None):
        _logger.info('sleep before request')
        time.sleep(1)
        return super(runbot_repo, self).github(cr, uid, ids, url, payload=payload, ignore_errors=ignore_errors, context=context)


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

    def _get_updated_modules(self, cr, uid, ids, field_name, arg, context=None):
        r = dict.fromkeys(ids, False)
        for bid in ids:
            files = self._get_pull_files(cr, uid, [bid], context=context)
            if files:
                files = [f['raw_url'] for f in files]
                files = [f.split('/') for f in files]
                updated_modules = set([f[7] for f in files if len(f) > 8])
                r[bid] = ','.join(updated_modules)
        return r

    def _get_pull_base_name(self, cr, uid, ids, field_name, arg, context=None):
        r = dict.fromkeys(ids, False)
        for bid in ids:
            pi = self._get_pull_info(cr, uid, [bid], context=context)
            if pi:
                r[bid] = pi['base']['ref']
        return r

    _columns = {
        #'pull_base_name': fields.function(_get_pull_base_name, type='char', string='PR Base name', readonly=1, store=True),
        'updated_modules': fields.function(_get_updated_modules, type='char', string='Updated modules', help='Comma-separated list of updated modules (for PR)', readonly=1, store=False),
    }


class runbot_build(orm.Model):
    _inherit = "runbot.build"

    def job_10_test_base(self, cr, uid, build, lock_path, log_path):
        build._log('test_base', 'skipping test_base')
        return MAGIC_PID_RUN_NEXT_JOB

    def job_20_test_all(self, cr, uid, build, lock_path, log_path):
        build._log('test_all', 'custom job_20_test_all (install updated modules)')
        self._local_pg_createdb(cr, uid, "%s-all" % build.dest)
        cmd, mods = build.cmd()
        if grep(build.server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-all' % build.dest, '-i', openerp.tools.ustr(mods), '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
        # reset job_start to an accurate job_20 job_time
        build.write({'job_start': now()})
        return self.spawn(cmd, lock_path, log_path, cpu_limit=2100)

    def checkout(self, cr, uid, ids, context=None):
        for build in self.browse(cr, uid, ids, context=context):
            # starts from scratch
            if os.path.isdir(build.path()):
                shutil.rmtree(build.path())

            # runbot log path
            mkdirs([build.path("logs"), build.server('addons')])

            # checkout branch
            build.branch_id.repo_id.git_export(build.name, build.path())

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
            explicit_modules = set(modules_to_test)
            _logger.debug("manual modules_to_test for build %s: %s", build.dest, modules_to_test)

            if not has_server:
                if build.repo_id.modules_auto == 'repo':
                    modules_to_test += [
                        os.path.basename(os.path.dirname(a))
                        for a in glob.glob(build.path('*/__openerp__.py'))
                    ]
                    _logger.debug("local modules_to_test for build %s: %s", build.dest, modules_to_test)

                for extra_repo in build.repo_id.dependency_ids:
                    if build.repo_id.is_addons_dev:
                        # addons-yelizariev-9.0-some-feature -> addons-yelizariev
                        repo_name = None
                        try:
                            repo_name = re.match('([^0-9]*)-', build.branch_id.pull_head_name).group(1)

                        except:
                            pass

                        if repo_name and extra_repo.name.endswith('%s.git' % repo_name):
                            _logger.debug('ignore repo "%s" as all modules are already in addons-dev branch' % repo_name)
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
                    for module in glob.glob(build.path('*/__openerp__.py'))
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

            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in glob.glob(build.server('addons/*/__openerp__.py'))
            ]
            if build.repo_id.modules_auto == 'all' or (build.repo_id.modules_auto != 'none' and has_server):
                modules_to_test += available_modules

            if build.repo_id.install_updated_modules:
                updated_modules = build.branch_id.updated_modules
                if updated_modules:
                    modules_to_test += updated_modules.split(',')

            modules_to_test = self.filter_modules(cr, uid, modules_to_test,
                                                  set(available_modules), explicit_modules)
            _logger.debug("modules_to_test for build %s: %s", build.dest, modules_to_test)
            build._log('checkout', 'modules to install: %s' % modules_to_test) 
            build.write({'server_match': server_match,
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
        pi = branch._get_pull_info()
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

        sort_by_repo = lambda d: (target_repo_ids.index(d['repo_id'][0]), -1 * len(d.get('branch_name', '')), -1 * d['id'])
        result_for = lambda d: (d['repo_id'][0], d['name'], 'exact')

        # 1. same name, not a PR
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('branch_name', '=', name),
            ('name', '=like', 'refs/heads/%'),
        ]
        targets = branch_pool.search_read(cr, uid, domain, ['name', 'repo_id'], order='id DESC',
                                          context=context)
        targets = sorted(targets, key=sort_by_repo)
        if targets:
            return result_for(targets[0])

        # 2. PR with head name equals
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('pull_head_name', '=', name),
            ('name', '=like', 'refs/pull/%'),
        ]
        pulls = branch_pool.search_read(cr, uid, domain, ['name', 'repo_id'], order='id DESC',
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
            ['name', 'branch_name', 'repo_id'], order='id DESC', context=context
        )
        branches = sorted(branches, key=sort_by_repo)

        for branch in branches:
            if name.startswith(branch['branch_name'] + '-'):
                return result_for(branch)

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
                build.path('openerp/addons')
            ])
            cmd += ['--addons-path', addons_path]

        if not modules:
            modules = 'base'

        return cmd, modules

    def _local_pg_dropdb(self, cr, uid, dbname):
        openerp.service.db._drop_conn(cr, dbname)
        super(runbot_build, self)._local_pg_dropdb(cr, uid, dbname)

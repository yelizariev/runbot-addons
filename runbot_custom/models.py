import re

from openerp.osv import orm, fields

MAGIC_PID_RUN_NEXT_JOB = -2

class runbot_repo(orm.Model):
    _inherit = "runbot.repo"

    _columns = {
        'is_addons_dev': fields.boolean('addons-dev'),
        'install_updated_modules': fields.boolean('Install updated modules'),
    }

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
                updated_modules = set([f[7] for f in files])
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
        'pull_base_name': fields.function(_get_pull_base_name, type='char', string='PR Base name', readonly=1, store=True),
        'updated_modules': fields.function(_get_updated_modules, type='char', string='Updated modules', help='Comma-separated list of updated modules (for PR)', readonly=1, store=False),
    }


class runbot_build(orm.Model):
    _inherit = "runbot.build"

    _columns = {
        'updated_modules': fields.text('Updated modules', readonly=True),
        'source_branch_name': fields.char('Source Branch Name', readonly=True),
        'target_branch_name': fields.char('Target Branch Name', readonly=True),
    }

    def sub_cmd(self, build, cmd):
        cmd = super(runbot_build, self).sub_cmd(build, cmd)
        internal_vals = {
            'pull_base_name': build.build_id.pull_base_name or '',
            'pull_head_name': build.build_id.pull_head_name or '',
        }
        return [i % internal_vals for i in cmd]

    def job_10_test_base(self, cr, uid, build, lock_path, log_path):
        _logger.info('skipping job_10_test_base')
        return MAGIC_PID_RUN_NEXT_JOB

    def job_20_test_all(self, cr, uid, build, lock_path, log_path):
        _logger.info('skipping job_20_test_all')
        return MAGIC_PID_RUN_NEXT_JOB

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
                        if repo_name and build.repo_id.name.endswith('%s.git' % repo_name):
                            # ignore repo as all modules are already in branch
                            continue
                    repo_id, closest_name, server_match = build._get_closest_branch_name(extra_repo.id)
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
                if build.branch_id.updated_modules:
                    modules_to_test += build.branch_id.updated_modules.split(',')

            modules_to_test = self.filter_modules(cr, uid, modules_to_test,
                                                  set(available_modules), explicit_modules)
            _logger.debug("modules_to_test for build %s: %s", build.dest, modules_to_test)
            build.write({'server_match': server_match,
                         'modules': ','.join(modules_to_test)})

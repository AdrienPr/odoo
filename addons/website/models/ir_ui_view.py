# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from itertools import groupby

from odoo import api, fields, models, _
from odoo import tools
from odoo.addons.http_routing.models.ir_http import url_for
from odoo.osv import expression
from odoo.http import request
from odoo.tools import pycompat

_logger = logging.getLogger(__name__)


class View(models.Model):

    _name = "ir.ui.view"
    _inherit = ["ir.ui.view", "website.seo.metadata"]

    customize_show = fields.Boolean("Show As Optional Inherit", default=False)
    website_id = fields.Many2one('website', ondelete='cascade', string="Website")
    page_ids = fields.One2many('website.page', 'view_id')
    first_page_id = fields.Many2one('website.page', string='Website Page', help='First page linked to this view', compute='_compute_first_page_id')
    theme_id = fields.Many2one('ir.module.module')

    @api.one
    def _compute_first_page_id(self):
        self.first_page_id = self.env['website.page'].search([('view_id', '=', self.id)], limit=1)

    @api.multi
    def write(self, vals):
        '''COW for ir.ui.view. This way editing websites does not impact other
        websites. Also this way newly created websites will only
        contain the default views.
        '''
        if not self._context.get('no_cow'):
            current_website_id = self._context.get('website_id')
            for view in self:
                currently_updating = self._context.get('install_mode_data', {}).get('module', '')
                if 'theme_' in currently_updating:
                    current_website_id = False
                    view = view._get_theme_specific_view(currently_updating)

                # if generic view in multi-website context
                if current_website_id and not view.website_id:
                    new_website_specific_view = view.copy({'website_id': current_website_id})
                    view._create_website_specific_pages_for_view(new_website_specific_view,
                                                                 view.env['website'].browse(current_website_id))

                    # trigger COW on inheriting views
                    for inherit_child in view.inherit_children_ids:
                        inherit_child.write({'inherit_id': new_website_specific_view.id})

                    new_website_specific_view.write(vals)
                else:
                    super(View, view).write(vals)
        else:
            super(View, self).write(vals)

        return True

    @api.multi
    def unlink(self):
        '''This implements COU (copy-on-unlink). When deleting a generic page
        website-specific pages will be created so only the current
        website is affected.
        '''
        current_website_id = self._context.get('website_id')

        if current_website_id and not self._context.get('no_cow'):
            for view in self.filtered(lambda view: not view.website_id):
                for website in self.env['website'].search([('id', '!=', current_website_id)]):
                    # reuse the COW mechanism to create
                    # website-specific copies, it will take
                    # care of creating pages and menus.
                    view.with_context(website_id=website.id).write({'key': '%s [website %s]' % (view.key, website.id)})

        self |= self.with_context(active_test=False).search([('key', 'in', self.filtered('key').mapped('key'))])
        result = super(View, self).unlink()
        self.clear_caches()
        return result

    @api.multi
    def _get_theme_specific_view(self, theme_name):
        self.ensure_one()
        view = self
        module_being_updated = self.env['ir.module.module'].search([('name', '=', theme_name)])
        xml_id = self.env['ir.model.data'].search([('model', '=', 'ir.ui.view'), ('res_id', '=', view.id)])
        if xml_id and xml_id.module != theme_name:
            _logger.info('%s is updating view %s (ID: %s)', theme_name, xml_id.complete_name, view.id)

            # check if a previously copied view for this theme already exists
            theme_specific_view = self.env['ir.ui.view'].search([('key', '=', view.key), ('theme_id', '=', module_being_updated.id)])
            if theme_specific_view:
                view = theme_specific_view
                _logger.info('diverting write to %s (ID: %s)', view.name, view.id)
            else:
                view = view.copy({'theme_id': module_being_updated.id})
                _logger.info('created new theme-specific view %s (ID: %s)', view.name, view.id)

        return view

    def _create_website_specific_pages_for_view(self, new_view, website):
        for page in self.page_ids:
            # create new pages for this view
            new_page = page.copy({
                'view_id': new_view.id,
            })
            for menu in page.menu_ids:
                # trigger COW
                menu.write({'page_id': new_page.id})

    @api.model
    def get_related_views(self, key, bundles=False):
        '''Make this only return most specific views for website.'''
        # get_related_views can be called through website=False routes
        # (e.g. /web_editor/get_assets_editor_resources), so website
        # dispatch_parameters may not be added. Manually set
        # website_id.
        self = self.with_context(website_id=self.env['website'].get_current_website().id)
        views = super(View, self).get_related_views(key, bundles=bundles)
        current_website_id = self._context.get('website_id')
        most_specific_views = self.env['ir.ui.view']

        if not current_website_id:
            return views

        for view in views:
            if view.website_id and view.website_id.id == current_website_id:
                most_specific_views |= view
            elif not view.website_id and not any(view.key == view2.key and view2.website_id and view2.website_id.id == current_website_id for view2 in views):
                most_specific_views |= view

        return most_specific_views

    @api.multi
    def _sort_suitability_key(self):
        """ Key function to sort views by descending suitability
            Suitability of a view is defined as follow:
                * if the view and request website_id are matched
                * then if the view has no set website
        """
        self.ensure_one()
        context_website_id = self.env.context.get('website_id', 1)
        website_id = self.website_id.id or 0
        different_website = context_website_id != website_id
        return (different_website, website_id)

    def filter_duplicate(self):
        """ Filter current recordset only keeping the most suitable view per distinct key """
        filtered = self.env['ir.ui.view']
        for dummy, group in groupby(self.sorted('key'), key=lambda record: record.key):
            filtered += sorted(group, key=lambda record: record._sort_suitability_key())[0]
        return filtered.sorted(key=lambda view: (view.priority, view.id))

    @api.model
    def _view_obj(self, view_id):
        if isinstance(view_id, pycompat.string_types):
            if 'website_id' in self._context:
                domain = [('key', '=', view_id)] + self.env['website'].website_domain(self._context.get('website_id'))
                order = 'website_id'
            else:
                domain = [('key', '=', view_id)]
                order = self._order
            views = self.search(domain, order=order)
            if views:
                return views.filter_duplicate()
            else:
                return self.env.ref(view_id)
        elif isinstance(view_id, pycompat.integer_types):
            return self.browse(view_id)

        # assume it's already a view object (WTF?)
        return view_id

    @api.model
    def _get_inheriting_views_arch_website(self, view_id):
        return self.env['website'].browse(self._context.get('website_id'))

    @api.model
    def _get_inheriting_views_arch_domain(self, view_id, model):
        domain = super(View, self)._get_inheriting_views_arch_domain(view_id, model)
        current_website = self._get_inheriting_views_arch_website(view_id)
        website_views_domain = current_website.website_domain()
        # when rendering for the website we have to include inactive views
        # we will prefer inactive website-specific views over active generic ones
        if current_website:
            domain = [leaf for leaf in domain if 'active' not in leaf]
            if current_website.theme_ids:
                theme_views_domain = [('theme_id', 'in', current_website.theme_ids.ids)]
                website_views_domain = expression.OR([website_views_domain, theme_views_domain])

        return expression.AND([website_views_domain, domain])

    @api.model
    def get_inheriting_views_arch(self, view_id, model):
        if not self._context.get('website_id'):
            return super(View, self).get_inheriting_views_arch(view_id, model)

        inheriting_views = super(View, self.with_context(active_test=False)).get_inheriting_views_arch(view_id, model)

        # prefer inactive website-specific views over active generic ones
        inheriting_views = self.browse([view[1] for view in inheriting_views]).filter_duplicate().filtered('active')

        return [(view.arch, view.id) for view in inheriting_views]

    @api.model
    @tools.ormcache_context('self._uid', 'xml_id', keys=('website_id',))
    def get_view_id(self, xml_id):
        if 'website_id' in self._context and not isinstance(xml_id, pycompat.integer_types):
            current_website = self.env['website'].browse(self._context.get('website_id'))
            key_domain = [('key', '=', xml_id)]
            theme_views_domain = [('theme_id', 'in', current_website.theme_ids.ids)]
            website_views_domain = [('theme_id', '=', False)] + current_website.website_domain()
            domain = expression.AND([expression.OR([theme_views_domain, website_views_domain]), key_domain])

            view = self.search(domain, order='website_id', limit=1)
            if not view:
                _logger.warning("Could not find view object with xml_id '%s'", xml_id)
                raise ValueError('View %r in website %r not found' % (xml_id, self._context['website_id']))
            return view.id
        return super(View, self).get_view_id(xml_id)

    @api.multi
    def render(self, values=None, engine='ir.qweb', minimal_qcontext=False):
        """ Render the template. If website is enabled on request, then extend rendering context with website values. """
        new_context = dict(self._context)
        if request and getattr(request, 'is_frontend', False):

            editable = request.website.is_publisher()
            translatable = editable and self._context.get('lang') != request.website.default_lang_code
            editable = not translatable and editable

            # in edit mode ir.ui.view will tag nodes
            if not translatable and not self.env.context.get('rendering_bundle'):
                if editable:
                    new_context = dict(self._context, inherit_branding=True)
                elif request.env.user.has_group('website.group_website_publisher'):
                    new_context = dict(self._context, inherit_branding_auto=True)

        if self._context != new_context:
            self = self.with_context(new_context)
        return super(View, self).render(values, engine=engine, minimal_qcontext=minimal_qcontext)

    @api.model
    def _prepare_qcontext(self):
        """ Returns the qcontext : rendering context with website specific value (required
            to render website layout template)
        """
        qcontext = super(View, self)._prepare_qcontext()

        if request and getattr(request, 'is_frontend', False):
            Website = self.env['website']
            editable = request.website.is_publisher()
            translatable = editable and self._context.get('lang') != request.env['ir.http']._get_default_lang().code
            editable = not translatable and editable

            if 'main_object' not in qcontext:
                qcontext['main_object'] = self

            domain_based_info = {'website_id': '', 'name': _('Domain Based')}
            force_website_id = request.session.get('force_website_id', False)
            if force_website_id:
                selected_website = Website.browse(force_website_id)
                qcontext['multi_website_selected_website'] = {'website_id': selected_website.id, 'name': selected_website.name}
            else:
                qcontext['multi_website_selected_website'] = domain_based_info

            qcontext['multi_website_websites'] = [{'website_id': website.id, 'name': website.name} for website in Website.search([])]
            qcontext['multi_website_websites'] += [domain_based_info]

            qcontext.update(dict(
                self._context.copy(),
                website=request.website,
                url_for=url_for,
                res_company=request.website.company_id.sudo(),
                default_lang_code=request.env['ir.http']._get_default_lang().code,
                languages=request.env['ir.http']._get_language_codes(),
                translatable=translatable,
                editable=editable,
                menu_data=self.env['ir.ui.menu'].load_menus_root() if request.website.is_user() else None,
            ))

        return qcontext

    @api.model
    def get_default_lang_code(self):
        website_id = self.env.context.get('website_id')
        if website_id:
            lang_code = self.env['website'].browse(website_id).default_lang_code
            return lang_code
        else:
            return super(View, self).get_default_lang_code()

    @api.multi
    def redirect_to_page_manager(self):
        return {
            'type': 'ir.actions.act_url',
            'url': '/website/pages',
            'target': 'self',
        }

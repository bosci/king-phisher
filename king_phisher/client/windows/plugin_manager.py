#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  king_phisher/client/windows/plugin_manager.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import collections
import datetime
import os
import shutil
import sys
import traceback

from king_phisher import utilities
from king_phisher.catalog import Catalog
from king_phisher.client import plugins
from king_phisher.client import gui_utilities
from king_phisher.client.widget import managers

from gi.repository import Gdk
from gi.repository import Gtk
import requests.exceptions

__all__ = ('PluginManagerWindow',)

_ROW_TYPE_PLUGIN = 'plugin'
_ROW_TYPE_REPOSITORY = 'repository'
_ROW_TYPE_CATALOG = 'catalog'
_LOCAL_REPOSITORY_ID = 'local'
_LOCAL_REPOSITORY_TITLE = '[Locally Installed]'

class PluginManagerWindow(gui_utilities.GladeGObject):
	"""
	The window which allows the user to selectively enable and disable plugins
	for the client application. This also handles configuration changes, so the
	enabled plugins will persist across application runs.
	"""
	dependencies = gui_utilities.GladeDependencies(
		children=(
			'expander_info',
			'grid_catalog_repo_info',
			'grid_plugin_info',
			'label_catalog_repo_info_title',
			'label_catalog_repo_info_description',
			'label_catalog_repo_info_for_description',
			'label_catalog_repo_info_homepage',
			'label_catalog_repo_info_maintainers',
			'label_catalog_repo_info_for_maintainers',
			'label_plugin_info_authors',
			'label_plugin_info_for_compatible',
			'label_plugin_info_compatible',
			'label_plugin_info_description',
			'label_plugin_info_homepage',
			'label_plugin_info_title',
			'label_plugin_info_version',
			'paned_plugins',
			'scrolledwindow_plugins',
			'stack_info',
			'treeview_plugins',
			'textview_plugin_info',
			'viewport_info',
			'statusbar'
		)
	)
	top_gobject = 'window'
	_named_model = collections.namedtuple(
		'model_row',
		[
			'id',
			'installed',
			'enabled',
			'title',
			'compatibility',
			'version',
			'visible_enabled',
			'visible_installed',
			'sensitive_installed',
			'type'
		]
	)
	def __init__(self, *args, **kwargs):
		super(PluginManagerWindow, self).__init__(*args, **kwargs)
		treeview = self.gobjects['treeview_plugins']
		self.status_bar = self.gobjects['statusbar']
		self._module_errors = {}
		tvm = managers.TreeViewManager(treeview, cb_refresh=self._load_plugins)
		toggle_renderer_enable = Gtk.CellRendererToggle()
		toggle_renderer_enable.connect('toggled', self.signal_renderer_toggled_enable)
		toggle_renderer_install = Gtk.CellRendererToggle()
		toggle_renderer_install.connect('toggled', self.signal_renderer_toggled_install)
		tvm.set_column_titles(
			['Installed', 'Enabled', 'Title', 'Compatible', 'Version'],
			column_offset=1,
			renderers=[
				toggle_renderer_install,
				toggle_renderer_enable,
				Gtk.CellRendererText(),
				Gtk.CellRendererText(),
				Gtk.CellRendererText()
			]
		)
		tvm.column_views['Enabled'].set_cell_data_func(toggle_renderer_enable, self._toggle_cell_data_func)
		tvm.column_views['Enabled'].add_attribute(toggle_renderer_enable, 'visible', 6)
		tvm.column_views['Enabled'].add_attribute(toggle_renderer_enable, 'sensitive', 1)
		tvm.column_views['Installed'].add_attribute(toggle_renderer_install, 'visible', 7)
		tvm.column_views['Installed'].add_attribute(toggle_renderer_install, 'sensitive', 8)
		self._model = Gtk.TreeStore(str, bool, bool, str, str, str, bool, bool, bool, str)
		self._model.set_sort_column_id(3, Gtk.SortType.ASCENDING)
		treeview.set_model(self._model)
		self.plugin_path = os.path.join(self.application.user_data_path, 'plugins')
		self.load_thread = utilities.Thread(target=self._load_catalogs)
		self.load_thread.start()
		self.popup_menu = tvm.get_popup_menu()
		self.popup_menu.append(Gtk.SeparatorMenuItem())
		menu_item = Gtk.MenuItem.new_with_label('Reload')
		menu_item.connect('activate', self.signal_popup_menu_activate_reload)
		self.popup_menu.append(menu_item)
		menu_item_reload_all = Gtk.MenuItem.new_with_label('Reload All')
		menu_item_reload_all.connect('activate', self.signal_popup_menu_activate_reload_all)
		self.popup_menu.append(menu_item_reload_all)
		self.popup_menu.show_all()
		self._update_status_bar('Loading...')
		self.window.show()

		selection = treeview.get_selection()
		selection.unselect_all()
		paned = self.gobjects['paned_plugins']
		self._paned_offset = paned.get_allocation().height - paned.get_position()

	def _treeview_unselect(self):
		treeview = self.gobjects['treeview_plugins']
		treeview.get_selection().unselect_all()

	def signal_window_show(self, _):
		pass

	def _load_catalogs(self, refresh=False):
		expiration = datetime.timedelta(hours=4)
		self._update_status_bar('Loading, catalogs...', idle=True)
		self.catalog_plugins = plugins.ClientCatalogManager(self.application.user_data_path)
		catalog_cache = self.catalog_plugins.get_cache()
		now = datetime.datetime.utcnow()
		for catalog_url in self.config['catalogs']:
			catalog_cache_dict = catalog_cache.get_catalog_by_url(catalog_url)
			if not refresh and catalog_cache_dict and catalog_cache_dict['created'] + expiration > now:
				try:
					catalog = Catalog(catalog_cache_dict['value'])
				except (KeyError, TypeError) as error:
					self.logger.warning("{0} error when trying to add catalog dict to manager".format(error.__class__.__name))
				else:
					self.catalog_plugins.add_catalog(catalog, catalog_url=catalog_cache_dict['url'], cache=False)
					continue
			self.logger.debug("downloading catalog: {}".format(catalog_url))
			self._update_status_bar("Loading, downloading catalog: {}".format(catalog_url))
			catalog = self._load_catalog_from_url(catalog_url)
			if not catalog:
				continue
			self.catalog_plugins.add_catalog(catalog, catalog_url=catalog_url, cache=True)
		self._load_plugins()

	def _load_catalog_from_url(self, catalog_url):
		catalog = None
		try:
			catalog = Catalog.from_url(catalog_url)
		except requests.exceptions.ConnectionError:
			self.logger.warning("connection error trying to download catalog url: {}".format(catalog_url))
			self.idle_show_dialog_error('Catalog Loading Error', "Failed to download catalog, check your internet connection.")
		except Exception:
			self.logger.warning("failed to add catalog by url", exc_info=True)
			self.idle_show_dialog_error('Catalog Loading Error', "Failed to add catalog")
		return catalog

	def idle_show_dialog_error(self, title, message):
		gui_utilities.glib_idle_add_once(gui_utilities.show_dialog_error, title, self.window, message)

	def __update_status_bar(self, string_to_set):
		self.status_bar.pop(0)
		self.status_bar.push(0, string_to_set)

	def _update_status_bar(self, string_to_set, idle=False):
		if idle:
			gui_utilities.glib_idle_add_once(self.__update_status_bar, string_to_set)
		else:
			self.__update_status_bar(string_to_set)

	def _set_model_item(self, model_path, item, item_value):
		self._model[model_path][self._named_model._fields.index(item)] = item_value

	def _on_plugin_load_error(self, name, error):
		self._module_errors[name] = (error, traceback.format_exception(*sys.exc_info(), limit=5))

	def _toggle_cell_data_func(self, column, cell, model, tree_iter, _):
		if model.get_value(tree_iter, 0) in self._module_errors:
			cell.set_property('inconsistent', True)
		else:
			cell.set_property('inconsistent', False)

	def _store_append(self, store, parent, model):
			return store.append(parent, model)

	def _store_extend(self, store, parent, models):
		for model in models:
			store.append(parent, model)

	def _load_plugins(self):
		"""
		Load the plugins which are available into the treeview to make them
		visible to the user.
		"""
		self.logger.debug('loading plugins')
		self._update_status_bar('Loading plugins...', idle=True)
		store = self._model
		store.clear()
		pm = self.application.plugin_manager
		self._module_errors = {}
		pm.load_all(on_error=self._on_plugin_load_error)
		model = (_LOCAL_REPOSITORY_ID, None, True, _LOCAL_REPOSITORY_TITLE, None, None, False, False, False, _ROW_TYPE_CATALOG)
		catalog_row = gui_utilities.glib_idle_add_wait(self._store_append, store, None, model)
		models = []
		for name, plugin in pm.loaded_plugins.items():
			if self.config['plugins.installed'].get(name):
				continue
			self.config['plugins.installed'][name] = None
			models.append(self._named_model(
				id=plugin.name,
				installed=True,
				enabled=plugin.name in pm.enabled_plugins,
				title=plugin.title,
				compatibility='Yes' if plugin.is_compatible else 'No',
				version=plugin.version,
				visible_enabled=True,
				visible_installed=True,
				sensitive_installed=False,
				type=_ROW_TYPE_PLUGIN
			))
		gui_utilities.glib_idle_add_once(self._store_extend, store, catalog_row, models)
		del models

		for name in self._module_errors.keys():
			model = (name, True, False, "{0} (Load Failed)".format(name), 'No', 'Unknown', True, True, False, _ROW_TYPE_PLUGIN)
			gui_utilities.glib_idle_add_once(self._store_append, store, catalog_row, model)

		self.logger.debug('loading catalog into plugin treeview')
		for catalog_id in self.catalog_plugins.catalog_ids():
			self._add_catalog_to_tree(catalog_id, store)

		catalog_cache = self.catalog_plugins.get_cache()
		for catalog_id in catalog_cache:
			if self.catalog_plugins.catalogs.get(catalog_id, None):
				continue
			named_catalog = catalog_cache[catalog_id]['value']
			model = (catalog_id, None, True, catalog_id, None, None, False, False, False, _ROW_TYPE_CATALOG)
			catalog_row = gui_utilities.glib_idle_add_wait(self._store_append, store, None, model)
			for repo in named_catalog.repositories:
				model = (repo.id, None, True, repo.title, None, None, False, False, False, _ROW_TYPE_REPOSITORY)
				repo_row = gui_utilities.glib_idle_add_wait(self._store_append, store, catalog_row, model)
				self._add_plugins_offline(catalog_id, repo.id, store, repo_row)

		gui_utilities.glib_idle_add_once(self._treeview_unselect)
		self._update_status_bar('Loading completed', idle=True)

	def _add_catalog_to_tree(self, catalog_id, store):
		model = self._named_model(
			id=catalog_id,
			installed=None,
			enabled=True,
			title=catalog_id,
			compatibility=None,
			version=None,
			visible_enabled=False,
			visible_installed=False,
			sensitive_installed=False,
			type=_ROW_TYPE_CATALOG
		)
		catalog_row = gui_utilities.glib_idle_add_wait(self._store_append, store, None, model)
		for repo in self.catalog_plugins.get_repositories(catalog_id):
			model = self._named_model(
				id=repo.id,
				installed=None,
				enabled=True,
				title=repo.title,
				compatibility=None,
				version=None,
				visible_enabled=False,
				visible_installed=False,
				sensitive_installed=False,
				type=_ROW_TYPE_REPOSITORY
			)
			repo_row = gui_utilities.glib_idle_add_wait(self._store_append, store, catalog_row, model)
			plugin_collections = self.catalog_plugins.get_collection(catalog_id, repo.id)
			if not plugin_collections:
				continue
			self._add_plugins_to_tree(catalog_id, repo, store, repo_row, plugin_collections)

	def _add_plugins_to_tree(self, catalog_id, repo, store, parent, plugin_list):
		models = []
		for plugin_info in plugin_list.values():
			installed = False
			enabled = False
			plugin_name = plugin_info['name']
			install_src = self.config['plugins.installed'].get(plugin_name)
			if install_src and repo.id == install_src['repo_id'] and catalog_id == install_src['catalog_id']:
				installed = True
				enabled = plugin_name in self.config['plugins.enabled']
			models.append(self._named_model(
				id=plugin_name,
				installed=installed,
				enabled=enabled,
				title=plugin_info['title'],
				compatibility='Yes' if self.catalog_plugins.is_compatible(catalog_id, repo.id, plugin_name) else 'No',
				version=plugin_info['version'],
				visible_enabled=True,
				visible_installed=True,
				sensitive_installed=self.catalog_plugins.is_compatible(catalog_id, repo.id, plugin_name),
				type=_ROW_TYPE_PLUGIN
			))
		gui_utilities.glib_idle_add_once(self._store_extend, store, parent, models)

	def _add_plugins_offline(self, catalog_id, repo_id, store, parent):
		models = []
		for plugin_name, plugin_src in self.config['plugins.installed'].items():
			if plugin_src is None:
				continue
			if plugin_name not in self.application.plugin_manager:
				continue
			if plugin_src['catalog_id'] != catalog_id:
				continue
			if plugin_src['repo_id'] != repo_id:
				continue
			models.append(self._named_model(
				id=plugin_name,
				installed=True,
				enabled=plugin_name in self.config['plugins.enabled'],
				title=self.application.plugin_manager[plugin_name].title,
				compatibility='Yes' if self.application.plugin_manager[plugin_name].is_compatible else 'No',
				version=self.application.plugin_manager[plugin_name].version,
				visible_enabled=True,
				visible_installed=True,
				sensitive_installed=False,
				type=_ROW_TYPE_PLUGIN
			))
		gui_utilities.glib_idle_add_once(self._store_extend, store, parent, models)

	def signal_popup_menu_activate_reload_all(self, _):
		if not self.load_thread.is_alive():
			self.load_thread = utilities.Thread(target=self._load_catalogs, kwargs={'refresh': True})
			self.load_thread.start()

	def signal_destory(self, _):
		pass

	def signal_treeview_row_activated(self, treeview, path, column):
		self._set_info(self._model[path])

	def signal_label_activate_link(self, _, uri):
		utilities.open_uri(uri)

	def signal_eventbox_button_press(self, widget, event):
		if not (event.type == Gdk.EventType.BUTTON_PRESS and event.button == Gdk.BUTTON_PRIMARY):
			return
		if not self._last_plugin_selected:
			return
		named_plugin = self._named_model(*self._last_plugin_selected)
		plugin_id = named_plugin.id
		if plugin_id is None:
			return
		if plugin_id in self.application.plugin_manager:
			klass = self.application.plugin_manager[plugin_id]
			compatibility_details = list(klass.compatibility)
		else:
			repo_model, catalog_model = self._get_plugin_model_parents(self._last_plugin_selected)
			compatibility_details = list(self.catalog_plugins.compatibility(catalog_model.id, repo_model.id, named_plugin.id))

		popover = Gtk.Popover()
		popover.set_relative_to(self.gobjects['label_plugin_info_for_compatible'])
		grid = Gtk.Grid()
		popover.add(grid)
		grid.insert_column(0)
		grid.insert_column(0)
		grid.insert_column(0)
		grid.set_column_spacing(3)

		compatibility_details.insert(0, ('Type', 'Value', 'Met'))
		row = 0
		for row, req in enumerate(compatibility_details):
			grid.insert_row(row)
			label = Gtk.Label(req[0])
			label.set_property('halign', Gtk.Align.START)
			grid.attach(label, 0, row, 1, 1)
			label = Gtk.Label(req[1])
			label.set_property('halign', Gtk.Align.START)
			grid.attach(label, 1, row, 1, 1)
			label = Gtk.Label(('Yes' if req[2] else 'No') if row else req[2])
			label.set_property('halign', Gtk.Align.END)
			grid.attach(label, 2, row, 1, 1)
		if not row:
			popover.destroy()
			return
		popover.show_all()

	def signal_expander_activate(self, expander):
		paned = self.gobjects['paned_plugins']
		if expander.get_property('expanded'):  # collapsing
			paned.set_position(paned.get_allocation().height + self._paned_offset)

	def signal_paned_button_press_event(self, paned, event):
		return not self.gobjects['expander_info'].get_property('expanded')

	def signal_popup_menu_activate_reload(self, _):
		if not self.load_thread.is_alive():
			self.load_thread = utilities.Thread(target=self._reload)
			self.load_thread.start()

	def _reload(self):
		self._update_status_bar("Reloading... ")
		treeview = self.gobjects['treeview_plugins']
		pm = self.application.plugin_manager
		selected_plugin = None
		selection = treeview.get_selection()
		if selection.count_selected_rows():
			(model, tree_paths) = selection.get_selected_rows()
			selected_plugin = model[tree_paths[0]][0]

		for tree_iter in gui_utilities.gtk_treeview_selection_iterate(treeview):
			model_row = self._model[tree_iter]
			# only reloading installed plugins is currently supported
			named_row = self._named_model(*model_row)
			if named_row.type == _ROW_TYPE_CATALOG and named_row.id != _LOCAL_REPOSITORY_ID:
				self._reload_catalog(named_row, tree_iter)
			elif named_row.type == _ROW_TYPE_REPOSITORY:
				self._reload_repository(model_row)
			elif named_row.type == _ROW_TYPE_PLUGIN:
				if not named_row.installed:
					self._update_status_bar('Cannot reload a plugin that is not installed.')
					continue
				self._reload_plugin(named_row, model_row, pm, tree_iter, selected_plugin)
			else:
				self.logger.warning('reload selected for an unsupported row type')

	def _reload_plugin(self, named_row, model_row, pm, tree_iter, selected_plugin):
		enabled = named_row.id in pm.enabled_plugins
		pm.unload(named_row.id)
		try:
			klass = pm.load(named_row.id, reload_module=True)
		except Exception as error:
			self._on_plugin_load_error(named_row.id, error)
			if named_row.id == selected_plugin:
				self._set_info(model_row)
			self._set_model_item(tree_iter, 'title', "{0} (Reload Failed)".format(named_row.id))
			return
		if named_row.id in self._module_errors:
			del self._module_errors[named_row.id]
		self._set_model_item(tree_iter, 'title', klass.title)
		self._set_model_item(tree_iter, 'compatibility', 'Yes' if klass.is_compatible else 'No')
		self._set_model_item(tree_iter, 'version', klass.version)
		if named_row.id == selected_plugin:
			self._set_info(self._model[tree_iter])
		if enabled:
			pm.enable(named_row.id)

	def _reload_catalog(self, named_row, tree_iter):
		self._model.remove(tree_iter)
		catalog_url = self.catalog_plugins.get_cache().get_catalog_by_id(named_row.id)['url']
		if not catalog_url:
			return
		catalog = self._load_catalog_from_url(catalog_url)
		if not catalog:
			return
		self.catalog_plugins.add_catalog(catalog, catalog_url=catalog_url, cache=True)
		self._load_plugins()

	def _reload_repository(self, model_row):
		parent_row = model_row.parent
		parent_named_row = self._named_model(*parent_row)
		if parent_named_row.type != _ROW_TYPE_CATALOG:
			self.logger.warning('repository treeview row\'s parent is not a catalog')
			return
		return self._reload_catalog(parent_named_row, parent_row.iter)

	def signal_renderer_toggled_enable(self, _, path):
		pm = self.application.plugin_manager
		named_row = self._named_model(*self._model[path])
		if named_row.type != _ROW_TYPE_PLUGIN:
			return
		if named_row.id not in pm.loaded_plugins:
			return

		if named_row.id in self._module_errors:
			gui_utilities.show_dialog_error('Can Not Enable Plugin', self.window, 'Can not enable a plugin which failed to load.')
			return
		if named_row.enabled:
			self._disable_plugin(path)
		else:
			if not pm.loaded_plugins[named_row.id].is_compatible:
				gui_utilities.show_dialog_error('Incompatible Plugin', self.window, 'This plugin is not compatible.')
				return
			if not pm.enable(named_row.id):
				return
			self._set_model_item(path, 'enabled', True)
			self.config['plugins.enabled'].append(named_row.id)

	def signal_renderer_toggled_install(self, _, path):
		repo_model, catalog_model = self._get_plugin_model_parents(self._model[path])
		named_row = self._named_model(*self._model[path])
		if named_row.installed:
			self._update_status_bar("Uninstalling plugin {}...".format(named_row.id))
			if named_row.enabled:
				if not gui_utilities.show_dialog_yes_no('Plugin is Enabled', self.window, 'This will disable the plugin, do you want to continue?'):
					return
				self._disable_plugin(path)
			self._uninstall_plugin(path)
			self._update_status_bar("Uninstalling plugin {} completed.".format(named_row.id))
			return

		if named_row.id in self.config['plugins.installed']:
			plugin_src = self.config['plugins.installed'].get(named_row.id)
			if plugin_src != {'catalog_id': catalog_model.id, 'repo_id': repo_model.id, 'plugin_id': named_row.id}:
				window_question = 'A plugin with this name is already installed from another\nrepository. Do you want to replace it with this one?'
				if not gui_utilities.show_dialog_yes_no('Plugin installed from another source', self.window, window_question):
					return
				if not self._remove_matching_plugin(path, plugin_src):
					self.logger.warning("failed to uninstall plugin {0}".format(named_row.id))
					return

		self._update_status_bar("Installing plugin {}...".format(named_row.title))
		try:
			self.catalog_plugins.install_plugin(catalog_model.id, repo_model.id, named_row.id, self.plugin_path)
		except requests.exceptions.ConnectionError:
			self.logger.warning("failed to download plugin {}".format(named_row.id))
			gui_utilities.show_dialog_error('Failed To Install', self.window, "Failed to download {} plugin, check your internet connection.".format(named_row.id))
			return
		except Exception:
			self.logger.warning("failed to install plugin {}".format(named_row.id), exc_info=True)
			gui_utilities.show_dialog_error('Failed To Install', self.window, "Failed to install {} plugin.".format(named_row.id))
			self._update_status_bar("Installing plugin {} failed.".format(named_row.title))
			return

		self.config['plugins.installed'][named_row.id] = {'catalog_id': catalog_model.id, 'repo_id': repo_model.id, 'plugin_id': named_row.id}
		self._set_model_item(path, 'installed', True)
		self._set_model_item(path, 'version', self.catalog_plugins.get_collection(catalog_model.id, repo_model.id)[named_row.id]['version'])
		self.logger.info("installed plugin {} from catalog:{}, repository:{}".format(named_row.id, catalog_model.id, repo_model.id))
		self._update_status_bar("Installing plugin {} completed.".format(named_row.title))
		self.application.plugin_manager.load_all(on_error=self._on_plugin_load_error)

	def _disable_plugin(self, path, is_path=True):
		named_row = self._named_model(*(self._model[path] if is_path else path))
		self.application.plugin_manager.disable(named_row.id)
		self.config['plugins.enabled'].remove(named_row.id)
		if is_path:
			self._set_model_item(path, 'enabled', False)
		else:
			path[self._named_model._fields.index('enabled')] = False

	def _remove_matching_plugin(self, path, plugin_src):
		named_row = self._named_model(*self._model[path])
		repo_model = None
		for catalog_model in self._model:
			catalog_id = self._named_model(*catalog_model).id
			if plugin_src and catalog_id == plugin_src['catalog_id']:
				repo_model = next((rm for rm in catalog_model.iterchildren() if self._named_model(*rm).id == plugin_src['repo_id']), None)
				break
			elif plugin_src is None and catalog_id == _LOCAL_REPOSITORY_ID:
				# local installation acts as a pseudo-repository
				repo_model = catalog_model
				break
		if not repo_model:
			return False
		for plugin_model in repo_model.iterchildren():
			named_model = self._named_model(*plugin_model)
			if named_model.id != named_row.id:
				continue
			if named_model.enabled:
				self._disable_plugin(plugin_model, is_path=False)
			self._uninstall_plugin(plugin_model.path)
			return True
		return False

	def _get_plugin_model_parents(self, plugin_model):
		return self._named_model(*plugin_model.parent), self._named_model(*plugin_model.parent.parent)

	def _uninstall_plugin(self, model_path):
		model_row = self._model[model_path]
		plugin_id = self._named_model(*model_row).id
		if os.path.isfile(os.path.join(self.plugin_path, plugin_id, '__init__.py')):
			shutil.rmtree(os.path.join(self.plugin_path, plugin_id))
		elif os.path.isfile(os.path.join(self.plugin_path, plugin_id + '.py')):
			os.remove(os.path.join(self.plugin_path, plugin_id + '.py'))
		else:
			self.logger.warning("failed to find plugin {0} on disk for removal".format(plugin_id))
			return False
		self.application.plugin_manager.unload(plugin_id)
		del self.config['plugins.installed'][plugin_id]

		if model_row.parent and model_row.parent[self._named_model._fields.index('id')] == _LOCAL_REPOSITORY_ID:
			del self._model[model_path]
		else:
			self._set_model_item(model_path, 'installed', False)
		self.logger.info("successfully uninstalled plugin {0}".format(plugin_id))
		return True

	def _set_info(self, model_instance):
		named_model = self._named_model(*model_instance)
		stack = self.gobjects['stack_info']
		textview = self.gobjects['textview_plugin_info']
		buf = textview.get_buffer()
		buf.delete(buf.get_start_iter(), buf.get_end_iter())
		model_id = named_model.id
		if named_model.type == _ROW_TYPE_PLUGIN:
			if model_id in self._module_errors:
				stack.set_visible_child(textview)
				self._set_info_plugin_error(model_instance)
			else:
				stack.set_visible_child(self.gobjects['grid_plugin_info'])
				self._set_info_plugin(model_instance)
		else:
			self._set_info_nonplugin(model_instance)

	def _set_info_nonplugin(self, model_instance):
		stack = self.gobjects['stack_info']
		stack.set_visible_child(self.gobjects['grid_catalog_repo_info'])
		named_model = self._named_model(*model_instance)
		obj_catalog = None
		self._hide_catalog_repo_labels()
		self.gobjects['label_catalog_repo_info_title'].set_text(named_model.title)
		if not named_model.id:
			return
		if named_model.type == _ROW_TYPE_CATALOG:
			obj = self.catalog_plugins.catalogs.get(named_model.id, None)
			if not obj:
				return
		else:
			obj_catalog = self.catalog_plugins.catalogs.get(self._named_model(*model_instance.parent).id, None)
			if not obj_catalog:
				return
			obj = self.catalog_plugins.catalogs[self._named_model(*model_instance.parent).id].repositories[named_model.id]

		maintainers = getattr(obj, 'maintainers', getattr(obj_catalog, 'maintainers', None))
		if maintainers:
			self.gobjects['label_catalog_repo_info_maintainers'].set_text('\n'.join(maintainers))
			self.gobjects['label_catalog_repo_info_maintainers'].set_property('visible', True)
			self.gobjects['label_catalog_repo_info_for_maintainers'].set_property('visible', True)
		if getattr(obj, 'description', None):
			self.gobjects['label_catalog_repo_info_description'].set_text(obj.description)
			self.gobjects['label_catalog_repo_info_description'].set_property('visible', True)
			self.gobjects['label_catalog_repo_info_for_description'].set_property('visible', True)
		if getattr(obj, 'homepage', None) or getattr(obj, 'url', None):
			url = getattr(obj, 'homepage', getattr(obj, 'url', None))
			self.gobjects['label_catalog_repo_info_homepage'].set_markup("<a href=\"{0}\">Homepage</a>".format(url))
			self.gobjects['label_catalog_repo_info_homepage'].set_property('tooltip-text', url)
			self.gobjects['label_catalog_repo_info_homepage'].set_property('visible', True)

	def _set_info_plugin(self, plugin_model):
		named_model = self._named_model(*plugin_model)
		pm = self.application.plugin_manager
		self._last_plugin_selected = plugin_model
		if named_model.id in pm.loaded_plugins:
			plugin = pm.loaded_plugins[named_model.id].metadata
			is_compatible = plugin['is_compatible']
		else:
			repo_model, catalog_model = self._get_plugin_model_parents(plugin_model)
			plugin = self.catalog_plugins.get_collection(catalog_model.id, repo_model.id)[named_model.id]
			is_compatible = self.catalog_plugins.is_compatible(catalog_model.id, repo_model.id, named_model.id)

		self.gobjects['label_plugin_info_title'].set_text(plugin['title'])
		self.gobjects['label_plugin_info_compatible'].set_text('Yes' if is_compatible else 'No')
		self.gobjects['label_plugin_info_version'].set_text(plugin['version'])
		self.gobjects['label_plugin_info_authors'].set_text('\n'.join(plugin['authors']))
		self.gobjects['label_plugin_info_description'].set_text(plugin['description'])
		self._set_homepage_url(plugin['homepage'])

	def _set_info_plugin_error(self, model_instance):
		id_ = self._named_model(*model_instance).id
		textview = self.gobjects['textview_plugin_info']
		buf = textview.get_buffer()
		exc, formatted_exc = self._module_errors[id_]
		buf.insert(buf.get_end_iter(), "{0!r}\n\n".format(exc), -1)
		buf.insert(buf.get_end_iter(), ''.join(formatted_exc), -1)

	def _hide_catalog_repo_labels(self):
		self.gobjects['label_catalog_repo_info_maintainers'].set_property('visible', False)
		self.gobjects['label_catalog_repo_info_for_maintainers'].set_property('visible', False)
		self.gobjects['label_catalog_repo_info_description'].set_property('visible', False)
		self.gobjects['label_catalog_repo_info_for_description'].set_property('visible', False)
		self.gobjects['label_catalog_repo_info_homepage'].set_property('visible', False)

	def _set_homepage_url(self, url=None):
		label_homepage = self.gobjects['label_plugin_info_homepage']
		if url is None:
			label_homepage.set_property('visible', False)
			return
		label_homepage.set_markup("<a href=\"{0}\">Homepage</a>".format(url))
		label_homepage.set_property('tooltip-text', url)
		label_homepage.set_property('visible', True)

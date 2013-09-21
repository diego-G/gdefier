"""gDefier module"""

__author__ = 'Diego Garcia (diego.gmartin@alumnos.uc3m.es)'

import jinja2
import jinja2.exceptions
import os
import pprint
import json
import yaml
import logging
from . import gDefier_model

from controllers import utils
from controllers import sites
from controllers.utils import BaseHandler
from controllers.utils import BaseRESTHandler
from controllers.utils import XsrfTokenManager
from controllers.sites import ApplicationContext

from models import content
from models import custom_modules
from models import vfs
from models import courses
from models import roles
from models import transforms
from models.courses import deep_dict_merge

from tools import verify

from modules.oeditor import oeditor

from modules.dashboard import filer
from modules.dashboard import messages
from modules.dashboard.course_settings import CourseSettingsRights


from google.appengine.api import users

GCB_GDEFIER_FOLDER_NAME = os.path.normpath('/modules/gDefier/')
DFR_CONFIG_FILENAME = os.path.normpath('/gDefier.yaml')

custom_module = None

def is_editable_fs(app_context):
    return app_context.fs.impl.__class__ == vfs.DatastoreBackedFileSystem

def get_gDefier_config_filename(self):
        """Returns absolute location of a course configuration file."""
        filename = sites.abspath(self.app_context.get_home_folder(),
                                  DFR_CONFIG_FILENAME)
        return filename

class StudentDefierHandler(BaseHandler):
    """Handlers of gDefier Module for student workspace"""
    def get(self):
        """Handles GET requests."""
        if not self.personalize_page_and_get_enrolled():
            return
        
        path = sites.abspath(self.app_context.get_home_folder(),
                     GCB_GDEFIER_FOLDER_NAME)
        
        course = sites.get_course_for_current_request()
        if not course.get_slug().split("_")[-1] == "DFR":
            page = 'templates/gDefier_error.html'
            self.template_value['error_code'] = 'disabled_gDefier_module'
            self.template_value['is_dashboard'] = False
        else:
            page = 'templates/gDefier.html'
            
        template = self.get_template(page, additional_dirs=[path])
        self.template_value['navbar'] = {'gDefier': True}
        self.render(template)

class GDefierDashboardHandler(object):
    """Should only be inherited by DashboardHandler, not instantiated."""

    def get_gDefier(self):
        """Renders course indexing view if G-Defier module is enabled."""
        
        template_values = {}
        mc_template_value = {}
        
        course = sites.get_course_for_current_request()
        template_values['page_title'] = self.format_title('GDefier')

        """ Catching url hackers """
        if not course.get_slug().split("_")[-1] == "DFR":
            mc_template_value['is_dashboard'] = True
            mc_template_value['error_code'] = 'disabled_gDefier_module'
            template_values['main_content'] = jinja2.Markup(self.get_template(
                'templates/gDefier_error.html', [os.path.dirname(__file__)]
                ).render(mc_template_value, autoescape=True))
            self.render_page(template_values)        
            return
        
        gDefier_actions = []

        # Enable editing if supported.
        if filer.is_editable_fs(self.app_context):
            gDefier_actions.append({
                'id': 'edit_gDefier_DB',
                'caption': 'Edit',
                'action': self.get_action_url('edit_gDefier_course_settings'),
                'xsrf_token': self.create_xsrf_token(
                    'edit_gDefier_course_settings')})

        # gDefier.yaml file content.
        gDefier_info = []

        yaml_stream = self.app_context.fs.open(get_gDefier_config_filename(self))
        
        if yaml_stream:
            yaml_lines = yaml_stream.read().decode('utf-8')
            for line in yaml_lines.split('\n'):
                gDefier_info.append(line)
        else:
            gDefier_info.append('< empty file >')

        # Prepare template values.
        template_values['sections'] = [
            {
                'title': 'Contents of DATA table from gDefier DB',
                'description': "General settings for G-Defier Module to this course.",
                'actions': gDefier_actions,
                'children': gDefier_info}]

        self.render_page(template_values)

class GDefierSettingsHandler(object):
    """G-Defier settings handler."""

    def post_edit_gDefier_course_settings(self):
        """Handles editing of DATA table from gDefier.db"""
        assert is_editable_fs(self.app_context)

        # Check if gDefier.yaml exists; create if not.
        fs = self.app_context.fs.impl
        course_yaml = fs.physical_to_logical('/gDefier.yaml')

        if not fs.isfile(course_yaml):
            fs.put(course_yaml, vfs.string_to_stream(
                courses.EMPTY_COURSE_YAML % users.get_current_user().email()))
        
        self.redirect(self.get_action_url(
            'edit_gDefier_settings', key='/gDefier.yaml'))

    def get_edit_gDefier_settings(self):
        """Shows editor for DATA table from gDefier.db"""

        key = self.request.get('key')

        exit_url = self.canonicalize_url('/dashboard?action=gDefier')
        rest_url = self.canonicalize_url('/rest/course/gDefier')

        form_html = oeditor.ObjectEditor.get_html_for(
            self,
            GDefierSettingsRESTHandler.REGISTORY.get_json_schema(),
            GDefierSettingsRESTHandler.REGISTORY.get_schema_dict(),
            key, rest_url, exit_url)

        template_values = {}
        template_values['page_title'] = self.format_title('Edit G-Defier Settings')
        template_values['page_description'] = 'These are the settings of this course for G-Defier Module.'
        template_values['main_content'] = form_html
        self.render_page(template_values)
        
class GDefierSettingsRESTHandler(BaseRESTHandler):
    """Provides REST API for a file."""
    
    REGISTORY = gDefier_model.create_gdefier_module_registry()
    
    URI = '/rest/course/gDefier'
    
    def get_course_dict(self):
        return self.get_environ2(self)

    @classmethod
    def validate_content(cls, content):
        yaml.safe_load(content)
    
    def get(self):
        """Handles REST GET verb and returns an object as JSON payload."""
        assert is_editable_fs(self.app_context)

        key = self.request.get('key')

        if not CourseSettingsRights.can_view(self):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': key})
            return
        
        # Load data if possible.
        fs = self.app_context.fs.impl
        filename = fs.physical_to_logical(key)

        try:
            stream = fs.get(filename)
        except:  # pylint: disable=bare-except
            stream = None
        if not stream:
            transforms.send_json_response(
                self, 404, 'Object not found.', {'key': key})
            return

        # Prepare data.
        entity = {}
        GDefierSettingsRESTHandler.REGISTORY.convert_entity_to_json_entity(
            self.get_course_dict(), entity)

        # Render JSON response.
        json_payload = transforms.dict_to_json(
            entity,
            GDefierSettingsRESTHandler.REGISTORY.get_json_schema_dict())
        
        transforms.send_json_response(
            self, 200, 'Success.',
            payload_dict=json_payload,
            xsrf_token=XsrfTokenManager.create_xsrf_token(
                'basic-course-settings-put'))

    def put(self):
        """Handles REST PUT verb with JSON payload."""
        assert is_editable_fs(self.app_context)

        request = transforms.loads(self.request.get('request'))
        key = request.get('key')

        if not self.assert_xsrf_token_or_fail(
                request, 'basic-course-settings-put', {'key': key}):
            return
        
        if not CourseSettingsRights.can_edit(self):
            transforms.send_json_response(
                self, 401, 'Access denied.', {'key': key})
            return

        payload = request.get('payload')

        request_data = {}
        GDefierSettingsRESTHandler.REGISTORY.convert_json_to_entity(
            transforms.loads(payload), request_data)

        entity = courses.deep_dict_merge(request_data, self.get_course_dict())
        content = yaml.safe_dump(entity)

        try:
            self.validate_content(content)
            content_stream = vfs.string_to_stream(unicode(content))
        except Exception as e:  # pylint: disable=W0703
            transforms.send_json_response(self, 412, 'Validation error: %s' % e)
            return

        # Store new file content.
        fs = self.app_context.fs.impl
        filename = fs.physical_to_logical(key)
        fs.put(filename, content_stream)

        # Send reply.
        transforms.send_json_response(self, 200, 'Saved.')
        

    def get_environ2(self, app_context2):
        """Returns currently defined course settings as a dictionary."""
        course_yaml = None
        course_yaml_dict = None
        
        ns = ApplicationContext.get_namespace_name_for_request()
        app_context = sites.get_app_context_for_namespace(ns)
        
        print app_context
        course_data_filename = sites.abspath(app_context.get_home_folder(), DFR_CONFIG_FILENAME)
        print course_data_filename
        if app_context.fs.isfile(course_data_filename):
            course_yaml = app_context.fs.open(course_data_filename)
            print course_yaml
        if not course_yaml:
            return deep_dict_merge(gDefier_model.DEFAULT_COURSE_GDEFIER_DICT,
                                   [])
        try:
            course_yaml_dict = yaml.safe_load(
                course_yaml.read().decode('utf-8'))
        except Exception as e:  # pylint: disable-msg=broad-except
            logging.info(
                'Error: gDefier.yaml file at %s not accessible, '
                'loading defaults. %s', course_data_filename, e)

        if not course_yaml_dict:
            return deep_dict_merge(gDefier_model.DEFAULT_COURSE_GDEFIER_DICT,
                                   [])
        return deep_dict_merge(
            course_yaml_dict, gDefier_model.DEFAULT_COURSE_GDEFIER_DICT)
        

def register_module():
    """Registers this module in the registry."""

    # provide parser to verify
    verify.parse_content = content.parse_string_in_scope

    # setup routes
    gDefier_routes = [
        ('/gDefier/home', StudentDefierHandler),
        ]

    global custom_module
    custom_module = custom_modules.Module(
        'gDefier',
        'A set of pages for using gDefier Module.',
        [], gDefier_routes)
    return custom_module

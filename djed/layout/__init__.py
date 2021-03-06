import re
import json
import logging
import random
import venusian
from collections import namedtuple
from collections import OrderedDict
from zope.interface import providedBy, Interface
from pyramid.compat import string_types
from pyramid.config.views import DefaultViewMapper
from pyramid.location import lineage
from pyramid.registry import Introspectable
from pyramid.renderers import RendererHelper
from pyramid.interfaces import IRequest, IRouteRequest
from pyramid.tweens import EXCVIEW


log = logging.getLogger('djed.layout')

LAYOUT_ID = 'djed:layout'

LayoutInfo = namedtuple(
    'LayoutInfo', 'name layout view original renderer intr')

CodeInfo = namedtuple(
    'Codeinfo', 'filename lineno function source module')


class ILayout(Interface):
    """ marker interface """


def query_layout(root, context, request, name=''):
    """ query named layout for context """
    assert IRequest.providedBy(request), "must pass in a request object"

    try:
        iface = request.request_iface
    except AttributeError:
        iface = IRequest

    root = providedBy(root)

    adapters = request.registry.adapters

    for context in lineage(context):
        layout_factory = adapters.lookup(
            (root, iface, providedBy(context)), ILayout, name=name)

        if layout_factory is not None:
            return layout_factory, context

    return None, None


def query_layout_chain(root, context, request, layoutname=''):
    chain = []

    layout, layoutcontext = query_layout(root, context, request, layoutname)
    if layout is None:
        return chain

    chain.append((layout, layoutcontext))
    contexts = {layoutname: layoutcontext}

    while layout is not None and layout.layout is not None:
        if layout.layout in contexts:
            l_context = contexts[layout.layout].__parent__
        else:
            l_context = context

        layout, layoutcontext = query_layout(
            root, l_context, request, layout.layout)
        if layout is not None:
            chain.append((layout, layoutcontext))
            contexts[layout.name] = layoutcontext

            if layout.layout is None:
                break

    return chain


def add_layout(cfg, name='', context=None, root=None, parent=None,
               renderer=None, route_name=None, use_global_views=True,
               view=None):
    """Registers a layout.

    :param name: Layout name
    :param context: Specific context for this layout.
    :param root:  Root object
    :param parent: A parent layout. None means no parent layout.
    :param renderer: A pyramid renderer
    :param route_name: A pyramid route_name. Apply layout only for
        specific route
    :param use_global_views: Apply layout to all routes. even is route
        doesnt use use_global_views.
    :param view: View callable

    """

    discr = (LAYOUT_ID, name, context, route_name)

    intr = Introspectable(LAYOUT_ID, discr, name, 'djed:layout')

    intr['name'] = name
    intr['context'] = context
    intr['root'] = root
    intr['renderer'] = renderer
    intr['route_name'] = route_name
    intr['parent'] = parent
    intr['use_global_views'] = use_global_views
    intr['view'] = view

    if not parent:
        parent = None
    elif parent == '.':
        parent = ''

    if isinstance(renderer, string_types):
        renderer = RendererHelper(name=renderer, registry=cfg.registry)

    if context is None:
        context = Interface

    def register():
        request_iface = IRequest
        if route_name is not None:
            request_iface = cfg.registry.getUtility(
                IRouteRequest, name=route_name)

        if use_global_views:
            request_iface = Interface

        mapper = getattr(view, '__view_mapper__', DefaultViewMapper)
        mapped_view = mapper()(view)

        info = LayoutInfo(name, parent, mapped_view, view, renderer, intr)
        cfg.registry.registerAdapter(
            info, (root, request_iface, context), ILayout, name)

    cfg.action(discr, register, introspectables=(intr,))


class LayoutRenderer(object):

    def __init__(self, layout):
        self.layout = layout

    def layout_info(self, layout, context, request, content):
        intr = layout.intr
        view = intr['view']
        if view is not None:
            layout_factory = '%s.%s'%(view.__module__, view.__name__)
        else:
            layout_factory = 'None'

        data = OrderedDict(
            (('name', intr['name']),
             ('parent-layout', intr['parent']),
             ('layout-factory', layout_factory),
             ('renderer', intr['renderer']),
             ('context', '%s.%s'%(context.__class__.__module__,
                                  context.__class__.__name__)),
             ('context-path', request.resource_url(context)),
             ))

        html = re.search('<html\.*>', content)
        color = random.randint(0,0xFFFFFF)

        if html:
            pos = html.end() - 1
            content = ('{0} style="border: 2px solid #{1:06x}" title="{2}"'
                       '{3}').format(content[:pos], color, data['name'],
                                     content[pos:])
        else:
            content = ('<div style="border: 2px solid #{0:06x}" title="{1}">'
                       '{2}</div>').format(color, data['name'], content)

        content = '\n<!-- layout:\n{0} \n-->\n{1}'.format(
            json.dumps(data, indent=2), content)

        return content

    def __call__(self, content, context, request):
        chain = query_layout_chain(request.root, context, request, self.layout)
        if not chain:
            log.warning(
                "Can't find layout '%s' for context '%s'",
                self.layout, context)
            return content

        value = request.layout_data

        for layout, layoutcontext in chain:
            if layout.view is not None:
                vdata = layout.view(layoutcontext, request)
                if vdata is not None:
                    value.update(vdata)

            system = {'view': getattr(request, '__view__', None),
                      'renderer_info': layout.renderer,
                      'context': layoutcontext,
                      'request': request,
                      'content': content,
                      'wrapped_content': content}

            content = layout.renderer.render(value, system, request)

            if request.registry.settings.get('djed.layout.debug'):
                content = self.layout_info(
                    layout, layoutcontext, request, content)

        return content


def set_layout_data(request, **kw):
    request.layout_data.update(kw)


class layout_config(object):

    def __init__(self, name='', context=None, root=None, parent=None,
                 renderer=None, route_name=None, use_global_views=True):
        self.name = name
        self.context = context
        self.root = root
        self.parent = parent
        self.renderer = renderer
        self.route_name = route_name
        self.use_global_views = use_global_views

    def __call__(self, wrapped):
        def callback(context, name, ob):
            cfg = context.config.with_package(info.module)
            add_layout(cfg, self.name, self.context,
                       self.root, self.parent,
                       self.renderer, self.route_name,
                       self.use_global_views, ob)

        info = venusian.attach(wrapped, callback, category='djed:layout')

        return wrapped


class layout_tween_factory(object):
    def __init__(self, handler, registry):
        self.handler = handler
        self.registry = registry

    def __call__(self, request):
        response = self.handler(request)

        layout_name = getattr(request, 'layout', None)
        if layout_name:
            layout = LayoutRenderer(layout_name)
            response.text = layout(response.text, request.context, request)

        return response


class layout_predicate_factory(object):
    def __init__(self, val, config):
        self.val = val

    def text(self):
        return 'layout = %s' % (self.val,)

    phash = text

    def __call__(self, context, request):
        request.layout = self.val
        return True


def includeme(config):
    from pyramid.settings import asbool

    settings = config.registry.settings
    settings['djed.layout.debug'] = asbool(settings.get(
        'djed.layout.debug', 'f'))

    config.add_tween('djed.layout.layout_tween_factory', over=EXCVIEW)
    config.add_view_predicate('layout', layout_predicate_factory)
    config.add_directive('add_layout', add_layout)
    config.add_request_method(set_layout_data, 'set_layout_data')

    def get_layout_data(request):
        return {}
    config.add_request_method(get_layout_data, 'layout_data', True, True)

'''
Core functionalities for the GUI web application (processing)

:date: Jul 31, 2016

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
'''

# make the following(s) behave like python3 counterparts if running from python2.7.x
# (http://python-future.org/imports.html#explicit-imports):
from builtins import map, zip

import re
from itertools import cycle

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from stream2segment.io.db.pdsql import colnames
from stream2segment.io.db.models import Segment, Class, Station, Channel, DataCenter, Event,\
    ClassLabelling, Download
from stream2segment.gui.webapp.mainapp.plots.jsplot import jsontimestamp
# from stream2segment.io.db import sqlevalexpr
from stream2segment.utils.resources import yaml_load_doc, get_templates_fpath
from stream2segment.io.db.sqlevalexpr import exprquery, inspect_instance, inspect_model
from sqlalchemy.orm import load_only
from stream2segment.process.utils import getseg


NPTS_WIDE = 900  # FIXME: automatic retrieve by means of Segment class relationships?
NPTS_SHORT = 900  # FIXME: see above


def get_segments(session, conditions, orderby, metadata, classes):
    classes = get_classes(session) if classes else []
    _metadata = []
    if metadata:
        _metadata = [[n, t, conditions.get(n, '')] for n, t in get_metadata(session)]
    # parse the orderby if it has a minus at the end it's descending:
    oby = orderby if not orderby else \
        [(k, "asc") if not k[-1] == '-' else (k[:-1], "desc") for k in orderby]
    qry = query4gui(session, conditions=conditions, orderby=oby)
    return {'segment_ids': [seg[0] for seg in qry],
            'classes': classes,
            'metadata': _metadata}


def query4gui(session, conditions, orderby=None):
    '''Returns a query yielding the segments ids for the visualization in the GUI (processing)
    according to `conditions` and `orderby`, sorted by default (if orderby is None) by
    segment's event.time (descending) and then segment's event_distance_deg (ascending)

    :param session: the sql-alchemy session
    :param condition: a dict of segment attribute names mapped to a select expression, each
    identifying a filter (sql WHERE clause). See `:ref:sqlevalexpr.py`. Can be empty (no filter)
    :param orderby: if None, defaults to segment's event.time (descending) and then
    segment's event_distance_deg (ascending). Otherwise, a list of tuples, where the first
    tuple element is a segment attribute (in string format) and the second element is either 'asc'
    (ascending) or 'desc' (descending)
    :return: a query yielding the tuples: ```(Segment.id)```
    '''
    if orderby is None:
        orderby = [('event.time', 'desc'), ('event_distance_deg', 'asc')]
    return exprquery(session.query(Segment.id), conditions=conditions, orderby=orderby,
                     distinct=True)


def get_metadata(session, seg_id=None):
    '''Returns a list of tuples (column, column_type) if `seg_id` is None or
    (column, column_value) if segment is not None. In the first case, `column_type` is the
    string representation of the column python type (str, datetime,...), in the latter,
    it is the value of `segment` for that column'''
    excluded_colnames = set([Station.inventory_xml, Segment.data, Download.log,
                             Download.config, Download.errors, Download.warnings,
                             Download.program_version, Class.description])

    if seg_id is not None:
        # exclude all classes attributes (returned in get_classes):
        excluded_colnames |= {Class.id, Class.label}
        segment = getseg(session, seg_id)
        if not segment:
            return []
        else:
            return inspect_instance(segment, exclude=excluded_colnames)
    else:
        # return inspect_model but convert types into their names (json serializable)
        return [[aname, getattr(aval, "__name__", "unknown")] for aname, aval in
                inspect_model(Segment, exclude=excluded_colnames)]


def set_class_id(session, segment_id, class_id, value):
    segment = getseg(session, segment_id)
    annotator = 'web app labeller'  # FIXME: use a session username or the computer username?
    if value:
        segment.add_classes(class_id, annotator=annotator)
    else:
        segment.del_classes(class_id)
    return {}


def get_classes(session, seg_id=None):
    '''If seg_id is not None, returns a list of the segment class ids.
    Otherwise, a list of dicts where each dict is a db row in the form
    {table_column: row_value}. The dict will have also a "count" attribute
    denoting how many segments have that class set'''
    if seg_id is not None:
        segment = getseg(session, seg_id)
        return [] if not segment else sorted(c.id for c in segment.classes)

    colnames = [Class.id.key, Class.label.key, 'count']
    # Note isouter which produces a left outer join, important when we have no class labellings
    # (i.e. third column all zeros) otherwise with a normal join we would have no results
    data = session.query(Class.id, Class.label, func.count(ClassLabelling.id).label(colnames[-1])).\
        join(ClassLabelling, ClassLabelling.class_id == Class.id, isouter=True).group_by(Class.id).\
        order_by(Class.id)
    return [{name: val for name, val in zip(colnames, d)} for d in data]


def get_segment_data(session, seg_id, plotmanager, plot_indices, all_components, preprocessed,
                     zooms, metadata=False, classes=False, sn_wdws=False):
    """Returns the segment data, depending on the arguments
    :param session: a flask sql-alchemy session object
    :param seg_id: integer denoting the segment id
    :param plotmanager: a PlotManager object, storing all plots data and sn/windows data
    :param plot_indices: a list of plots to be calculated from the given `plotmanager` (which caches
    its plot for performance speed)
    :param all_components: boolean, whether or not the `plotmanager` should give all components for
    the main plot (plot representing the given segment's data, whose plot index is currently 0).
    Ignored if 0 is not in `plot_indices`
    :param preprocessed: boolean, whether or not the `plotmanager` should calculate the plots on
    the pre-processing function defined in the config (if any), or on the raw obspy Stream
    :param zooms: a list of **all plots** defined in the plotmanager, or None.
    Each element is either None, or a tuple of [xmin, xmax] values (xmin and xmax can be both None,
    to conform python slicing behaviour). Thus, the length of `zooms` most likely differs from
    that of `plot_indices`. the zooms of interest are, roughly speaking,
    [zooms[i] for i in plot_indices] (if zoom is not None)
    :param metadata: boolean, whether or not to return a list of the segment metadata. The list
    is a list of tuples ('column', value). A list is used to preserve order for client-side
    javascript parsing
    :param classes: boolean, whether to return the integers classes ids (if any) of the given
    segment
    :param sn_wdws: boolean, whether to returns the sn windows calculated according to the
    config values. The returned list is a 2-element list, where each element is in turn a
    2-element numeric list: [noise_window_start, noise_window_end],
    [signal_window_start, signal_window_end]
    """
    plots = []
    zooms_ = parse_zooms(zooms, plot_indices)
    sn_windows = []
    if sn_wdws:
        if sn_wdws['signal_window']:
            try:
                sn_wdws = {'signal_window': parse_array(sn_wdws['signal_window'], float),
                           'arrival_time_shift': float(sn_wdws['arrival_time_shift'])}
            except Exception:
                pass
        # set_sn_windows(self, session, a_time_shift, signal_window):
        plotmanager.update_config(sn_windows=sn_wdws)

    if plot_indices:
        plots = plotmanager.get_plots(session, seg_id, plot_indices, preprocessed, all_components)
        try:
            # return always sn_windows, as we already calculated them. IT is better
            # to call this method AFTER get_plots_func defined above
            sn_windows = [sorted([jsontimestamp(x[0]), jsontimestamp(x[1])])
                          for x in plotmanager.get_data(seg_id, 'sn_windows',
                                                        preprocessed, [])]
        except Exception:
            sn_windows = []

    return {'plots': [p.tojson(z, NPTS_WIDE) for p, z in zip(plots, zooms_)],
            'sn_windows': sn_windows,
            'metadata': [] if not metadata else get_metadata(session, seg_id),
            'classes': [] if not classes else get_classes(session, seg_id)}


def parse_array(str_array, parsefunc=None, try_return_scalar=True):
    '''splits str_array into elements, and apply func on each element
    :param str_array: a valid string array, with or without square brackets. Leading and
    trailing spaces will be ignored (str split is applied twice if the string has square
    brackets). The separation characters are the comma surrounded by zero or more spaces, or
    a one or more spaces. E.g. "  [1 ,3  ]", "[1,3]"
    '''
    # str_array should always be a string... just in case it's already a parsable value
    # (e.g., parsefunc = float and str-array = 5.6), then try to parse it first:
    if parsefunc is not None and try_return_scalar:
        try:
            return parsefunc(str_array)
        except Exception:
            pass
    d = str_array.strip()
    if d[0] == '[' and d[-1] == ']':
        d = d[1:-1].strip()
    _ = re.split("(?:\\s*,\\s*|\\s+)", d)
    if parsefunc is not None:
        _ = list(map(parsefunc, _))
    return _[0] if try_return_scalar and len(_) == 1 else _


def parse_zooms(zooms, plot_indices):
    '''parses the zoom received from the frontend. Basically, if any zoom is a string,
    tries to parse it to datetime
    :param zooms: a list of 2-element tuples, or None's. The elements of the tuple can be number,
    Nones or strings (in datetime format)
    :return: an iterator over zooms. Uses itertools cycle so that this method can be safely used
    with izip never estinguishing it
    '''
    if not zooms or not plot_indices:
        zooms = cycle([None, None])  # to be safe in iterations
    _zooms = []
    for plot_index in plot_indices:
        try:
            z = zooms[plot_index]
        except (IndexError, TypeError):
            z = [None, None]
        _zooms.append(z)
    return _zooms  # set zooms to None if length is not enough


def get_doc(key, plotmanager):
    '''returns the doc from the given key:
    :param plotmanager: the plotmanager. Used if key is 'preprocessfunc' (see below)
    :param key: 'preprocessfunc' (the doc will be the python doc implemented from the user)
    'sn_windows' (the doc will be parsed by the gui.yaml file implemented in resources folder),
    'segment_select' (the doc for the segment selection popup div)
    '''
    if key == 'preprocessfunc':
        ret = plotmanager.get_preprocessfunc_doc
        idx = ret.find("\n\n")
        if idx > -1:
            ret = ret[:idx]
    elif key == 'sn_windows':
        ret = yaml_load_doc(get_templates_fpath("processing.yaml"), "sn_windows", True)
    elif key == 'segment_select':
        ret = yaml_load_doc(get_templates_fpath("processing.yaml"), "segment_select", True)
        # remove the example session cause is misleading from the GUI (double string quotation
        # is not needed) and redundant (the form should be already self-explanatory):
        ret = re.sub("\\s+Example:.*$", "", ret, flags=re.DOTALL)  # @UndefinedVariable
    if not ret:
        ret = "error: documentation N/A"
    return ret.strip()
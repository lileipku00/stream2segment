# -*- encoding: utf-8 -*-
'''
Module with utilities for the download report

:date: Mar 15, 2018

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
'''
from __future__ import print_function

from collections import defaultdict
from future.standard_library import install_aliases
from future.utils import viewitems, itervalues, viewvalues, viewkeys

from urllib.parse import urlparse  # @IgnorePep8

from sqlalchemy.sql.expression import func, or_

from stream2segment.utils.resources import yaml_load
from stream2segment.utils import get_session, get_progressbar, StringIO
from stream2segment.io.db.models import Segment, concat, Station, DataCenter, Download, substr
from stream2segment.download.utils import custom_download_codes, DownloadStats

install_aliases()


def get_dstats_dicts(dburl, download_ids=None, maxgap_threshold=0.5, isterminal=False):

    sess = get_session(dburl)  # FIXME: create session in inputartgs instead???

    echo = lambda *arg, **kwarg: None if not isterminal else print  # @IgnorePep8 @UnusedVariable

    echo('Fetching data, please wait ...')

    # Benchmark: the bare minimum (with postgres on external server) request takes around 12
    # sec and 14 seconds adding all necessary information. Therefore, we choose the latter
    maxgap_bexpr = get_maxgap_sql_expr(maxgap_threshold)
    data = sess.query(func.count(Segment.id),
                      Station.id,
                      concat(Station.network, '.', Station.station),
                      Station.latitude,
                      Station.longitude,
                      Station.datacenter_id,
                      Segment.download_id,
                      Segment.download_code,
                      maxgap_bexpr).join(Segment.station)
    data = filterquery(data, download_ids).group_by(Station.id, Segment.download_id,
                                                    Segment.download_code, maxgap_bexpr,
                                                    Segment.datacenter_id)

    codesfound = set()

    # sta_data = {sta_name: [staid, stalat, stalon, sta_dcid,
    #                        {d_id: {code1: num_seg , codeN: num_seg}, ... }
    #                       ],
    #            ...,
    #            }
    sta_data = {}
    dstats2 = DownloadStats2()
    for segcount, staid, staname, lat, lon, dc_id, dwn_id, dwn_code, has_go in data:
        sta_list = sta_data.get(staname, [staid, round(lat, 2), round(lon, 2), dc_id, None])
        if sta_list[-1] is None:
            sta_list[-1] = defaultdict(lambda: defaultdict(int))
            sta_data[staname] = sta_list
        sta_dic = sta_list[-1][dwn_id]
        if dwn_code == 200 and has_go is True:
            dwn_code = dstats2.GAP_OVLAP_CODE
        sta_dic[dwn_code] += segcount
        codesfound.add(dwn_code)

    # In the html, we want to reduce all possible data, as the file might be huge
    # modify stas_data nested dicts, replacing codes with an incremental integer
    # and keep a separate list that maps uses codes to titles and legends
    # So, first sort codes and keep track of their index
    sortedcodes = DownloadStats2.sortcodes(codesfound)
    codeint = {k: i for i, k in enumerate(sortedcodes)}
    for values in viewvalues(sta_data):
        dwnlids2dict = values[-1]
        for did, code2numsegs in viewitems(dwnlids2dict):
            dwnlids2dict[did] = {codeint[code]: val for code, val in viewitems(code2numsegs)}

    codes = [dstats2.titlelegend(code) for code in sortedcodes]

    return sta_data, codes, get_datacenters(sess), get_downloads(sess)


def filterquery(query, download_ids=None):
    '''adds a filter to the given query if download_ids is not None, and returns a new
    query. Otherwise, if download_ids is None, it's no-op and returns query itself'''
    if download_ids is not None:
        query = query.filter(Segment.download_id.in_(download_ids))
    return query


def get_downloads(sess, download_ids=None):
    '''Returns a dict of download ids mapped to the tuple
    (download_run_time, download_eventws_query_args)
    the first element is a string, the second a dict
    '''
    query = filterquery(sess.query(Download.id, Download.run_time, Download.config), download_ids)
    return {did: (time.isoformat(), yaml_load(StringIO(cfg))['eventws_query_args'])
            for (did, time, cfg) in query}


def get_datacenters(sess):
    '''returns a dict of datacenters id mapped to the network location of their url'''
    return {did: urlparse(ds).netloc for (did, ds) in sess.query(DataCenter.id,
                                                                 DataCenter.dataselect_url)}


def get_maxgap_sql_expr(maxgap_threshold=0.5):
    '''returns a sql-alchemy binary expression which matches segments with gaps/overlaps,
    according to the given threshold'''
    return or_(Segment.maxgap_numsamples < -abs(maxgap_threshold),
               Segment.maxgap_numsamples > abs(maxgap_threshold))


class DownloadStats2(DownloadStats):

    def __init__(self):
        super(DownloadStats2, self).__init__()
        self.GAP_OVLAP_CODE = 200.1  # place it right after 200 OK responses and before -204 (some
        # chunks out of bounds)
        self.resp[self.GAP_OVLAP_CODE] = ('OK Gaps Overlaps',
                                          'Data saved (download ok, '
                                          'data has gaps or overlaps)')


def get_dstats_str_iter(dburl, download_ids=None, maxgap_threshold=0.5, isterminal=False):

    sess = get_session(dburl)  # FIXME: create session in inputartgs instead???

    # Benchmark: the bare minimum (with postgres on external server) request takes around 12
    # sec and 14 seconds adding all necessary information. Therefore, we choose the latter
    maxgap_bexpr = get_maxgap_sql_expr(maxgap_threshold)
    data = sess.query(func.count(Segment.id),
                      Segment.download_code,
                      Segment.datacenter_id,
                      Segment.download_id,
                      maxgap_bexpr)
    data = filterquery(data, download_ids).group_by(Segment.download_id, Segment.datacenter_id,
                                                    Segment.download_code, maxgap_bexpr)

    dcurl = get_datacenters(sess)
    agg_statz = DownloadStats2()
    stas = defaultdict(lambda: DownloadStats2())
    for segcount, dwn_code, dc_id, dwn_id, has_go in data:
        statz = stas[dwn_id]

        if dwn_code == 200 and has_go is True:
            dwn_code = agg_statz.GAP_OVLAP_CODE

        statz[dcurl[dc_id]][dwn_code] += segcount
        agg_statz[dcurl[dc_id]][dwn_code] += segcount

    dwlids = get_downloads(sess, download_ids)

    for did, dwl in viewitems(dwlids):
        yield ascii_decorate('Download id: %d' % did)
        yield 'executed: %s' % str(dwl[0])
        yield 'eventws query args:'
        for param in sorted(dwl[1]):
            yield " %s = %s" % (param, str(dwl[1][param]))
        yield ''
        yield str(stas.get(did, 'N/A'))
        yield ''

    if len(dwlids) > 1:
        yield ascii_decorate('Aggregated stats (all downloads)')
        yield ''
        yield str(agg_statz)


def ascii_decorate(string):
    leng = (len(string) + 2)
    firstline = "╔" + "═" * leng + "╗"
    secondline = "║ " + string + " ║"
    thirdline = "╚" + "═" * leng + "╝"
    return "\n".join([firstline, secondline, thirdline])

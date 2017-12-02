# -*- coding: utf-8 -*-
"""
Core functions and classes for the download routine

.. moduleauthor:: Riccardo Zaccarelli <rizac@gfz-potsdam.de>
"""

# make the following(s) behave like python3 counterparts if running from python2.7.x
# (http://python-future.org/imports.html#explicit-imports):
from builtins import map, next, zip, range, object

import sys
import os
import logging
from collections import OrderedDict
from datetime import timedelta, datetime
from itertools import cycle

import numpy as np
import pandas as pd
from sqlalchemy import or_, and_
import psutil

from stream2segment.utils.url import urlread, read_async as original_read_async, URLException
from stream2segment.io.db.models import Event, DataCenter, Segment, Station, Channel, \
    WebService, fdsn_urls
from stream2segment.io.db.pdsql import dfrowiter, mergeupdate, dbquery2df, DbManager,\
    syncdf, shared_colnames
from stream2segment.download.utils import empty, urljoin, response2df, normalize_fdsn_dframe, \
    get_search_radius, DownloadStats, get_events_list, locations2degrees, custom_download_codes, \
    eidarsiter, EidaValidator
from stream2segment.utils import strconvert, get_progressbar
from stream2segment.utils.mseedlite3 import MSeedError, unpack as mseedunpack
from stream2segment.utils.msgs import MSG
# from stream2segment.utils.resources import get_ws_fpath, yaml_load
from stream2segment.io.utils import dumps_inv
from stream2segment.io.db.queries import query4inventorydownload
from stream2segment.traveltimes.ttloader import TTTable
from stream2segment.utils.resources import get_ttable_fpath

# make the following(s) behave like python3 counterparts if running from python2.7.x
# (http://python-future.org/imports.html#aliased-imports):
from future import standard_library
standard_library.install_aliases()
from urllib.parse import urlparse  # @IgnorePep8
from urllib.request import Request  # @IgnorePep8


logger = logging.getLogger(__name__)


class QuitDownload(Exception):
    """
    This is an exception that should be raised from each function of this module, when their OUTPUT
    dataframe is empty and thus would prevent the continuation of the program.
    **Any function here THUS EXPECTS THEIR DATAFRAME INPUT TO BE NON-EMPTY.**

    There are two causes for having empty data(frame). In both cases, the program should exit,
    but the behavior should be different:

    - There is no data because of a download error (no data fetched):
      the program should `log.error` the message and return nonzero. Then, from the function
      that raises the exception write:

      ```raise QuitDownload(Exception(...))```

    - There is no data because of current settings (e.g., no channels with sample rate >=
      config sample rate, all segments already downloaded with current retry settings):
      the program should `log.info` the message and return zero. Then, from the function
      that raises the exception write:

      ```raise QuitDownload(string_message)```

    Note that in both cases the string messages need most likely to be built with the `MSG`
    function for harmonizing the message outputs.
    (Note also that with the current settings defined in stream2segment/main,
    `log.info` and `log.error` both print also to `stdout`, `log.warning` and `log.debug` do not).

    From within `run` (the caller function) one should `try.. catch` a function raising
    a `QuitDownload` and call `QuitDownload.log()` which handles the log and returns the
    exit code depending on how the `QuitDownload` was built:
    ```
        try:
            ... function raising QuitDownload ...
        catch QuitDownload as dexc:
            exit_code = dexc.log()  # print to log
            # now we can handle exit code. E.g., if we want to exit:
            if exit_code != 0:
                return exit_code
    ```
    """
    def __init__(self, exc_or_msg):
        """Creates a new QuitDownload instance
        :param exc_or_msg: if Exception, then this object will log.error in the `log()` method
        and return a nonzero exit code (error), otherwise (if string) this object will log.info
        inthere and return 0
        """
        super(QuitDownload, self).__init__(str(exc_or_msg))
        self._iserror = isinstance(exc_or_msg, Exception)

    def log(self):
        if self._iserror:
            logger.error(self)
            return 1  # that's the program return
        else:
            # use str(self) although MSG does not care
            # but in case the formatting will differ, as we are here not for an error,
            # we might be ready to distinguish the cases
            logger.info(str(self))
            return 0  # that's the program return, 0 means ok anyway


def read_async(iterable, urlkey=None, max_workers=None, blocksize=1024*1024,
               decode=None, raise_http_err=True, timeout=None, max_mem_consumption=90,
               **kwargs):
    """Wrapper around read_async defined in url which raises a QuitDownload in case of MemoryError
    :param max_mem_consumption: a value in (0, 100] denoting the threshold in % of the
    total memory after which the program should raise. This should return as fast as possible
    consuming the less memory possible, and assuring the quit-download message will be sent to
    the logger
    """
    # split the two cases, a little nit more verbose but hopefully it's faster when
    # max_mem_consumption is higher than zero...
    if max_mem_consumption > 0 and max_mem_consumption < 100:
        check_mem_step = 10
        process = psutil.Process(os.getpid())
        for result, check_mem_val in zip(original_read_async(iterable, urlkey, max_workers,
                                                             blocksize, decode,
                                                             raise_http_err, timeout, **kwargs),
                                         cycle(range(check_mem_step))):
            yield result
            if check_mem_val == 0:
                mem_percent = process.memory_percent()
                if mem_percent > max_mem_consumption:
                    raise QuitDownload(MemoryError(("Memory overflow: %.2f%% (used) > "
                                                    "%.2f%% (threshold)") %
                                                   (mem_percent, max_mem_consumption)))
    else:
        for result in original_read_async(iterable, urlkey, max_workers, blocksize, decode,
                                          raise_http_err, timeout, **kwargs):
            yield result


def dbsyncdf(dataframe, session, matching_columns, autoincrement_pkey_col, update=False,
             buf_size=10,
             drop_duplicates=True, return_df=True, cols_to_print_on_err=None):
    """Calls `syncdf` and writes to the logger before returning the
    new dataframe. Raises `QuitDownload` if the returned dataframe is empty (no row saved)"""

    oninsert_err_callback = handledbexc(cols_to_print_on_err, update=False)
    onupdate_err_callback = handledbexc(cols_to_print_on_err, update=True)
    onduplicates_callback = oninsert_err_callback

    inserted, not_inserted, updated, not_updated, df = \
        syncdf(dataframe, session, matching_columns, autoincrement_pkey_col, update,
               buf_size, drop_duplicates,
               onduplicates_callback, oninsert_err_callback, onupdate_err_callback)

    table = autoincrement_pkey_col.class_
    if empty(df):
        raise QuitDownload(Exception(MSG("No row saved to table '%s'" % table.__tablename__,
                                         "unknown error, check log for details and db connection")))
    dblog(table, inserted, not_inserted, updated, not_updated)
    return df


def handledbexc(cols_to_print_on_err, update=False):
    """Returns a **function** to be passed to pdsql functions when inserting/ updating
    the db. Basically, it prints to log"""
    if not cols_to_print_on_err:
        return None

    def hde(dataframe, exception):
        if not empty(dataframe):
            try:
                # if sql-alchemy exception, try to guess the orig atrribute which represents
                # the wrapped exception
                # http://docs.sqlalchemy.org/en/latest/core/exceptions.html
                errmsg = str(exception.orig)
            except AttributeError:
                # just use the string representation of exception
                errmsg = str(exception)
            len_df = len(dataframe)
            msg = MSG("%d database rows not %s" % (len_df, "updated" if update else "inserted"),
                      errmsg)
            logwarn_dataframe(dataframe, msg, cols_to_print_on_err)
    return hde


def logwarn_dataframe(dataframe, msg, cols_to_print_on_err, max_row_count=30):
    '''prints (using log.warning) the current dataframe. Does not check if dataframe is empty'''
    len_df = len(dataframe)
    if len_df > max_row_count:
        footer = "\n... (showing first %d rows only)" % max_row_count
        dataframe = dataframe.iloc[:max_row_count]
    else:
        footer = ""
    msg = "{}:\n{}{}".format(msg, dataframe.to_string(columns=cols_to_print_on_err,
                                                      index=False), footer)
    logger.warning(msg)


def dblog(table, inserted, not_inserted, updated=0, not_updated=0):
    """Prints to log the result of a database wrtie operation.
    Use this function to harmonize the message format and make it more readable in log or
    terminal"""

    _header = "Db table '%s'" % table.__tablename__
    if not inserted and not not_inserted and not updated and not not_updated:
        logger.info("%s: no new row to insert, no row to update", _header)
    else:
        def log(ok, notok, okstr, nookstr):
            if not ok and not notok:
                return
            _errmsg = "sql errors"
            _noerrmsg = "no sql error"
            msg = okstr % (ok, "row" if ok == 1 else "rows")
            infomsg = _noerrmsg
            if notok:
                msg += nookstr % notok
                infomsg = _errmsg
            logger.info(MSG("%s: %s" % (_header, msg), infomsg))

        log(inserted, not_inserted, "%d new %s inserted", ", %d discarded")
        log(updated, not_updated, "%d %s updated", ", %d discarded")


def get_events_df(session, eventws_url, db_bufsize, **args):
    """
        Returns the events from an event ws query. Splits the results into smaller chunks
        (according to 'start' and 'end' parameters, if they are not supplied in **args they will
        default to `datetime(1970, 1, 1)` and `datetime.utcnow()`, respectively)
        In case of errors just raise, the caller is responsible of displaying messages to the
        logger, which is used in this function only for all those messages which should not stop
        the program
    """
    eventws_id = session.query(WebService.id).filter(WebService.url == eventws_url).scalar()
    if eventws_id is None:  # write url to table
        data = [("event", eventws_url)]
        df = pd.DataFrame(data, columns=[WebService.type.key, WebService.url.key])
        df = dbsyncdf(df, session, [WebService.url], WebService.id, buf_size=db_bufsize)
        eventws_id = df.iloc[0][WebService.id.key]

    url = urljoin(eventws_url, format='text', **args)
    ret = []
    try:
        datalist = get_events_list(eventws_url, **args)
    except ValueError as exc:
        raise QuitDownload(exc)

    if len(datalist) > 1:
        logger.info(MSG("Request was split into sub-queries, aggregating the results",
                        "Original request entity too large", url))

    for data, msg, url in datalist:
        if not data and msg:
            logger.warning(MSG("Discarding request", msg, url))
        elif data:
            try:
                events_df = response2normalizeddf(url, data, "event")
                ret.append(events_df)
            except ValueError as exc:
                logger.warning(MSG("Discarding response", exc, url))

    if not ret:  # pd.concat below raise ValueError if ret is empty:
        raise QuitDownload(Exception(MSG("",
                                         "No events found. Check config and log for details",
                                         url)))

    events_df = pd.concat(ret, axis=0, ignore_index=True, copy=False)
    events_df[Event.webservice_id.key] = eventws_id
    events_df = dbsyncdf(events_df, session,
                         [Event.eventid, Event.webservice_id], Event.id, buf_size=db_bufsize,
                         cols_to_print_on_err=[Event.eventid.key])

    # try to release memory for unused columns (FIXME: NEEDS TO BE TESTED)
    return events_df[[Event.id.key, Event.magnitude.key, Event.latitude.key, Event.longitude.key,
                     Event.depth_km.key, Event.time.key]].copy()


def response2normalizeddf(url, raw_data, dbmodel_key):
    """Returns a normalized and harmonized dataframe from raw_data. dbmodel_key can be 'event'
    'station' or 'channel'. Raises ValueError if the resulting dataframe is empty or if
    a ValueError is raised from sub-functions
    :param url: url (string) or `Request` object. Used only to log the specified
    url in case of wranings
    """

    dframe = response2df(raw_data)
    oldlen, dframe = len(dframe), normalize_fdsn_dframe(dframe, dbmodel_key)
    # stations_df surely not empty:
    if oldlen > len(dframe):
        logger.warning(MSG("%d row(s) discarded",
                           "malformed server response data, e.g. NaN's", url),
                       oldlen - len(dframe))
    return dframe


def get_datacenters_df(session, service, routing_service_url,
                       channels, starttime=None, endtime=None,
                       db_bufsize=None):
    """Returns a 2 elements tuple: the dataframe of the datacenter(s) matching `service`,
    and an EidaValidator (built on the eida routing service response)
    for checking stations/channels duplicates after querying the datacenter(s)
    for stations / channels. If service != 'eida', this argument is None
    Note that channels, starttime, endtime can be all None and
    are used only if service = 'eida'
    :param service: the string denoting the dataselect *or* station url in fdsn format, or
    'eida', or 'iris'. In case of 'eida', `routing_service_url` must denote an url for the
    edia routing service. If falsy (e.g., empty string or None), `service` defaults to 'eida'
    """
    # For convenience and readability, define once the mapped column names representing the
    # dataframe columns that we need:
    DC_SURL = DataCenter.station_url.key
    DC_DURL = DataCenter.dataselect_url.key
    DC_ORG = DataCenter.organization_name.key

    eidavalidator = None
    eidars_responsetext = ''

    if not service:
        service = 'eida'

    if service.lower() == 'iris':
        IRIS_NETLOC = 'https://service.iris.edu'
        dc_df = pd.DataFrame(data={DC_DURL: '%s/fdsnws/dataselect/1/query' % IRIS_NETLOC,
                                   DC_SURL: '%s/fdsnws/station/1/query' % IRIS_NETLOC,
                                   DC_ORG: 'iris'}, index=[0])
    elif service.lower() != 'eida':
        fdsn_normalized = fdsn_urls(service)
        if fdsn_normalized:
            station_ws = fdsn_normalized[0]
            dataselect_ws = fdsn_normalized[1]
            dc_df = pd.DataFrame(data={DC_DURL: dataselect_ws,
                                       DC_SURL: station_ws,
                                       DC_ORG: None}, index=[0])
        else:
            raise QuitDownload(Exception(MSG("Unable to use datacenter",
                                             "Url does not seem to be a valid fdsn url", service)))
    else:
        dc_df, eidars_responsetext = get_eida_datacenters_df(session, routing_service_url,
                                                             channels, starttime, endtime)

    # attempt saving to db only if we might have something to save:
    if service != 'eida' or eidars_responsetext:  # not eida, or eida succesfully queried: Sync db
        dc_df = dbsyncdf(dc_df, session, [DataCenter.station_url], DataCenter.id,
                         buf_size=len(dc_df) if db_bufsize is None else db_bufsize)
        if eidars_responsetext:
            eidavalidator = EidaValidator(dc_df, eidars_responsetext)

    return dc_df, eidavalidator


def get_eida_datacenters_df(session, routing_service_url, channels, starttime=None, endtime=None):
    """Returns the tuple (datacenters_df, eidavalidator) from eidars or from the db (in this latter
    case eidavalidator is None)
    """
    # For convenience and readability, define once the mapped column names representing the
    # dataframe columns that we need:
    DC_SURL = DataCenter.station_url.key
    DC_DURL = DataCenter.dataselect_url.key
    DC_ORG = DataCenter.organization_name.key

    # do not return only new datacenters, return all of them
    query_args = {'service': 'dataselect', 'format': 'post'}
    if channels:
        query_args['channel'] = ",".join(channels)
    if starttime:
        query_args['start'] = starttime.isoformat()
    if endtime:
        query_args['end'] = endtime.isoformat()

    url = urljoin(routing_service_url, **query_args)
    dc_df = None
    dclist = []

    try:
        responsetext, status, msg = urlread(url, decode='utf8', raise_http_err=True)
        for url, postdata in eidarsiter(responsetext):  # @UnusedVariable
            urls = fdsn_urls(url)
            if urls:
                dclist.append({DC_SURL: urls[0], DC_DURL: urls[1], DC_ORG: 'eida'})
        if not dclist:
            raise URLException(Exception("No datacenters found in response text"))
        return pd.DataFrame(dclist), responsetext

    except URLException as urlexc:
        dc_df = dbquery2df(session.query(DataCenter.id, DataCenter.station_url,
                                         DataCenter.dataselect_url).
                           filter(DataCenter.organization_name == 'eida')).\
                                reset_index(drop=True)
        if empty(dc_df):
            msg = MSG("Eida routing service error, no eida data-center saved in database",
                      urlexc.exc, url)
            raise QuitDownload(Exception(msg))
        else:
            msg = MSG("Eida routing service error", urlexc.exc, url)
            logger.warning(msg)
            # logger.info(msg)
            return dc_df, None


def get_channels_df(session, datacenters_df, eidavalidator,  # <- can be none
                    channels, starttime, endtime,
                    min_sample_rate, update,
                    max_thread_workers, timeout, blocksize, db_bufsize,
                    show_progress=False):
    """Returns a dataframe representing a query to the eida services (or the internal db
    if `post_data` is None) with the given argument.  The
    dataframe will have as columns the `key` attribute of any of the following db columns:
    ```
    [Channel.id, Station.latitude, Station.longitude, Station.datacenter_id]
    ```
    :param datacenters_df: the first item resulting from `get_datacenters_df` (pandas DataFrame)
    :param post_data: the second item resulting from `get_datacenters_df` (string)
    :param channels: a list of string denoting the channels, or None for no filtering
    (all channels). Each string follows FDSN specifications (e.g. 'BHZ', 'H??'). This argument
    is not used if `post_data` is given (not None)
    :param min_sample_rate: minimum sampling rate, set to negative value for no-filtering
    (all channels)
    """
    postdata = "* * * %s %s %s" % (",".join(channels) if channels else "*",
                                   "*" if not starttime else starttime.isoformat(),
                                   "*" if not endtime else endtime.isoformat())
    ret = []
    url_failed_dc_ids = []
    iterable = ((id_, Request(url,
                              data=('format=text\nlevel=channel\n'+post_data_str).encode('utf8')))
                for url, id_, post_data_str in zip(datacenters_df[DataCenter.station_url.key],
                                                   datacenters_df[DataCenter.id.key],
                                                   cycle([postdata])))

    with get_progressbar(show_progress, length=len(datacenters_df)) as bar:
        for obj, result, exc, url in read_async(iterable, urlkey=lambda obj: obj[-1],
                                                blocksize=blocksize,
                                                max_workers=max_thread_workers,
                                                decode='utf8', timeout=timeout):
            bar.update(1)
            dcen_id = obj[0]
            if exc:
                url_failed_dc_ids.append(dcen_id)
                logger.warning(MSG("Unable to fetch stations", exc, url))
            else:
                try:
                    df = response2normalizeddf(url, result[0], "channel")
                except ValueError as exc:
                    logger.warning(MSG("Discarding response data", exc, url))
                    df = empty()
                if not empty(df):
                    df[Station.datacenter_id.key] = dcen_id
                    ret.append(df)

    db_cha_df = pd.DataFrame()
    if url_failed_dc_ids:  # if some datacenter does not return station, warn with INFO
        dc_df_fromdb = datacenters_df.loc[datacenters_df[DataCenter.id.key].isin(url_failed_dc_ids)]
        logger.info(MSG("Fetching stations from database for %d (of %d) data-center(s)",
                    "download errors occurred") %
                    (len(dc_df_fromdb), len(datacenters_df)) + ":")
        logger.info(dc_df_fromdb[DataCenter.dataselect_url.key].to_string(index=False))
        db_cha_df = get_channels_df_from_db(session, dc_df_fromdb, channels, starttime, endtime,
                                            min_sample_rate, db_bufsize)

    # build two dataframes which we will concatenate afterwards
    web_cha_df = pd.DataFrame()
    if ret:  # pd.concat complains for empty list
        web_cha_df = pd.concat(ret, axis=0, ignore_index=True, copy=False)
        # remove unmatching sample rates:
        if min_sample_rate > 0:
            srate_col = Channel.sample_rate.key
            oldlen, web_cha_df = len(web_cha_df), \
                web_cha_df[web_cha_df[srate_col] >= min_sample_rate]
            discarded_sr = oldlen - len(web_cha_df)
            if discarded_sr:
                logger.warning(MSG("%d channel(s) discarded",
                                   "sample rate < %s Hz" % str(min_sample_rate)),
                               discarded_sr)
            if web_cha_df.empty and db_cha_df.empty:
                raise QuitDownload("No channel found with sample rate >= %f" % min_sample_rate)

        try:
            # this raises QuitDownload if we cannot save any element:
            web_cha_df = save_stations_and_channels(session, web_cha_df, eidavalidator, update,
                                                    db_bufsize)
        except QuitDownload as qexc:
            if db_cha_df.empty:
                raise
            else:
                logger.warning(qexc)

    if web_cha_df.empty and db_cha_df.empty:
        # ok, now let's see if we have remaining datacenters to be fetched from the db
        raise QuitDownload(Exception(MSG("No station found",
                                     ("Unable to fetch stations from all data-centers, "
                                      "no data to fetch from the database. "
                                      "Check config and log for details"))))

    # the columns for the channels dataframe that will be returned
    colnames = [c.key for c in [Channel.id, Channel.station_id, Station.latitude,
                                Station.longitude, Station.datacenter_id, Station.start_time,
                                Station.end_time, Station.network, Station.station,
                                Channel.location, Channel.channel]]
    if db_cha_df.empty:
        return web_cha_df[colnames]
    elif web_cha_df.empty:
        return db_cha_df[colnames]
    else:
        return pd.concat((web_cha_df, db_cha_df), axis=0, ignore_index=True)[colnames].copy()


def get_channels_df_from_db(session, datacenters_df, channels, starttime, endtime, min_sample_rate,
                            db_bufsize):
    # _be means "binary expression" (sql alchemy object reflecting a sql clause)
    cha_be = or_(*[Channel.channel.like(strconvert.wild2sql(cha)) for cha in channels]) \
        if channels else True
    srate_be = Channel.sample_rate >= min_sample_rate if min_sample_rate > 0 else True
    # select only relevant datacenters. Convert tolist() cause python3 complains of numpy ints
    # (python2 doesn't but tolist() is safe for both):
    dc_be = Station.datacenter_id.in_(datacenters_df[DataCenter.id.key].tolist())
    # Starttime and endtime below: it must NOT hold:
    # station.endtime <= starttime OR station.starttime >= endtime
    # i.e. it MUST hold the negation:
    # station.endtime > starttime AND station.starttime< endtime
    stime_be = ((Station.end_time == None) | (Station.end_time > starttime)) if starttime else True  # @IgnorePep8
    # endtime: Limit to metadata epochs ending on or before the specified end time.
    # Note that station's ent_time can be None
    etime_be = (Station.start_time < endtime) if endtime else True  # @IgnorePep8
    sa_cols = [Channel.id, Channel.station_id, Station.latitude, Station.longitude,
               Station.start_time, Station.end_time, Station.datacenter_id, Station.network,
               Station.station, Channel.location, Channel.channel]
    # note below: binary expressions (all variables ending with "_be") might be the boolean True.
    # SqlAlchemy seems to understand them as long as they are preceded by a "normal" binary
    # expression. Thus q.filter(binary_expr & True) works and it's equal to q.filter(binary_expr),
    # BUT .filter(True & True) is not working as a no-op filter, it simply does not work
    qry = session.query(*sa_cols).join(Channel.station).filter(and_(dc_be, srate_be, cha_be,
                                                                    stime_be, etime_be))
    return dbquery2df(qry)


def save_stations_and_channels(session, channels_df, eidavalidator, update, db_bufsize):
    """
        Saves to db channels (and their stations) and returns a dataframe with only channels saved
        The returned data frame will have the column 'id' (`Station.id`) renamed to
        'station_id' (`Channel.station_id`) and a new 'id' column referring to the Channel id
        (`Channel.id`)
        :param channels_df: pandas DataFrame resulting from `get_channels_df`
    """
    # define columns (sql-alchemy model attrs) and their string names (pandas col names) once:
    STA_NET = Station.network.key
    STA_STA = Station.station.key
    STA_STIME = Station.start_time.key
    STA_DCID = Station.datacenter_id.key
    STA_ID = Station.id.key
    CHA_STAID = Channel.station_id.key
    CHA_LOC = Channel.location.key
    CHA_CHA = Channel.channel.key
    # set columns to show in the log on error (no row written):
    STA_ERRCOLS = [STA_NET, STA_STA, STA_STIME, STA_DCID]
    CHA_ERRCOLS = [STA_NET, STA_STA, CHA_LOC, CHA_CHA, STA_STIME, STA_DCID]
    # define a pre-formatteed string to log.info to in case od duplicates:
    infomsg = "Found {:d} {} to be discarded (checked against %s)" % \
        ("already saved stations: eida routing service n/a" if eidavalidator is None else
         "eida routing service response")
    # first drop channels of same station:
    sta_df = channels_df.drop_duplicates(subset=[STA_NET, STA_STA, STA_STIME, STA_DCID]).copy()
    # then check dupes. Same network, station, starttime but different datacenter:
    duplicated = sta_df.duplicated(subset=[STA_NET, STA_STA, STA_STIME], keep=False)
    if duplicated.any():
        sta_df_dupes = sta_df[duplicated]
        if eidavalidator is not None:
            keep_indices = []
            for _, group_df in sta_df_dupes.groupby(by=[STA_NET, STA_STA, STA_STIME],
                                                    sort=False):
                gdf = group_df.sort_values([STA_DCID])  # so we take first dc returning True
                for i, d, n, s, l, c in zip(gdf.index, gdf[STA_DCID], gdf[STA_NET], gdf[STA_STA],
                                            gdf[CHA_LOC], gdf[CHA_CHA]):
                    if eidavalidator.isin(d, n, s, l, c):
                        keep_indices.append(i)
                        break
            sta_df_dupes = sta_df_dupes.loc[~sta_df_dupes.index.isin(keep_indices)]
        else:
            sta_df_dupes.is_copy = False
            sta_df_dupes[STA_DCID + "_tmp"] = sta_df_dupes[STA_DCID].copy()
            sta_df_dupes[STA_DCID] = np.nan
            sta_db = dbquery2df(session.query(Station.network, Station.station, Station.start_time,
                                              Station.datacenter_id))
            mergeupdate(sta_df_dupes, sta_db, [STA_NET, STA_STA, STA_STIME], [STA_DCID])
            sta_df_dupes = sta_df_dupes[sta_df_dupes[STA_DCID] != sta_df_dupes[STA_DCID + "_tmp"]]

        if not sta_df_dupes.empty:
            exc_msg = "duplicated station(s)"
            logger.info(infomsg.format(len(sta_df_dupes), exc_msg))
            # print the removed dataframe to log.warning (showing STA_ERRCOLS only):
            handledbexc(STA_ERRCOLS)(sta_df_dupes.sort_values(by=[STA_NET, STA_STA, STA_STIME]),
                                     Exception(exc_msg))
            # https://stackoverflow.com/questions/28901683/pandas-get-rows-which-are-not-in-other-dataframe:
            sta_df = sta_df.loc[~sta_df.index.isin(sta_df_dupes.index)]

    # remember: dbsyncdf raises a QuitDownload, so no need to check for empty(dataframe)
    # also, if update is True, for stations only it must NOT update inventories HERE (handled later)
    _update_stations = update
    if _update_stations:
        _update_stations = [_ for _ in shared_colnames(Station, sta_df, pkey=False)
                            if _ != Station.inventory_xml.key]
    sta_df = dbsyncdf(sta_df, session, [Station.network, Station.station, Station.start_time],
                      Station.id, _update_stations, buf_size=db_bufsize, drop_duplicates=False,
                      cols_to_print_on_err=STA_ERRCOLS)
    # sta_df will have the STA_ID columns, channels_df not: set it from the former to the latter:
    channels_df = mergeupdate(channels_df, sta_df, [STA_NET, STA_STA, STA_STIME, STA_DCID],
                              [STA_ID])
    # rename now 'id' to 'station_id' before writing the channels to db:
    channels_df.rename(columns={STA_ID: CHA_STAID}, inplace=True)
    # check dupes and warn:
    channels_df_dupes = channels_df[channels_df[CHA_STAID].isnull()]
    if not channels_df_dupes.empty:
        exc_msg = "duplicated channel(s)"
        logger.info(infomsg.format(len(channels_df_dupes), exc_msg))
        # do not print the removed dataframe to log.warning (showing CHA_ERRCOLS only)
        # the info is redundant given the already removed stations. Left commented in any case:
        # handledbexc(CHA_ERRCOLS)(channels_df_dupes, Exception(exc_msg))
        channels_df.dropna(axis=0, subset=[CHA_STAID], inplace=True)
    # add channels to db:
    channels_df = dbsyncdf(channels_df, session,
                           [Channel.station_id, Channel.location, Channel.channel],
                           Channel.id, update, buf_size=db_bufsize, drop_duplicates=False,
                           cols_to_print_on_err=CHA_ERRCOLS)
    return channels_df


def chaid2mseedid_dict(channels_df, drop_mseedid_columns=True):
    '''returns a dict of the form {channel_id: mseed_id} from channels_df, where mseed_id is
    a string of the form ```[network].[station].[location].[channel]```
    :param channels_df: the result of `get_channels_df`
    :param drop_mseedid_columns: boolean (default: True), removes all columns related to the mseed
    id from `channels_df`. This might save up a lor of memory when cimputing the
    segments resulting from each event -> stations binding (according to the search radius)
    Remember that pandas strings are not optimized for memory as they are python objects
    (https://www.dataquest.io/blog/pandas-big-data/)
    '''
    # For convenience and readability, define once the mapped column names representing the
    # dataframe columns that we need:
    CHA_ID = Channel.id.key
    STA_NET = Station.network.key
    STA_STA = Station.station.key
    CHA_LOC = Channel.location.key
    CHA_CHA = Channel.channel.key

    n = channels_df[STA_NET].str.cat
    s = channels_df[STA_STA].str.cat
    l = channels_df[CHA_LOC].str.cat
    c = channels_df[CHA_CHA]
    _mseedids = n(s(l(c, sep='.', na_rep=''), sep='.', na_rep=''), sep='.', na_rep='')
    if drop_mseedid_columns:
        # remove string columns, we do not need it anymore and
        # will save a lot of memory for subsequent operations
        channels_df.drop([STA_NET, STA_STA, CHA_LOC, CHA_CHA], axis=1, inplace=True)
    # we could return
    # pd.DataFrame(index=channels_df[CHA_ID], {'mseed_id': _mseedids})
    # but the latter does NOT consume less memory (strings are python string in pandas)
    # and the search for an mseed_id given a loc[channel_id] is slower than python dicts.
    # As the returned element is intended for searching, then return a dict:
    return {chaid: mseedid for chaid, mseedid in zip(channels_df[CHA_ID], _mseedids)}


def merge_events_stations(events_df, channels_df, minmag, maxmag, minmag_radius, maxmag_radius,
                          tttable, show_progress=False):
    """
        Merges `events_df` and `channels_df` by returning a new dataframe representing all
        channels within a specific search radius. *Each row of the resturned data frame is
        basically a segment to be potentially donwloaded*.
        The returned dataframe will be the same as `channels_df` with one or more rows repeated
        (some channels might be in the search radius of several events), plus a column
        "event_id" (`Segment.event_id`) representing the event associated to that channel
        and two columns 'event_distance_deg', 'time' (representing the *event* time) and
        'depth_km' (representing the event depth in km)
        :param channels_df: pandas DataFrame resulting from `get_channels_df`
        :param events_df: pandas DataFrame resulting from `get_events_df`
    """
    # For convenience and readability, define once the mapped column names representing the
    # dataframe columns that we need:
    EVT_ID = Event.id.key
    EVT_MAG = Event.magnitude.key
    EVT_LAT = Event.latitude.key
    EVT_LON = Event.longitude.key
    EVT_TIME = Event.time.key
    EVT_DEPTH = Event.depth_km.key
    STA_LAT = Station.latitude.key
    STA_LON = Station.longitude.key
    STA_STIME = Station.start_time.key
    STA_ETIME = Station.end_time.key
    CHA_ID = Channel.id.key
    CHA_STAID = Channel.station_id.key
    SEG_EVID = Segment.event_id.key
    SEG_EVDIST = Segment.event_distance_deg.key
    SEG_ATIME = Segment.arrival_time.key
    SEG_DCID = Segment.datacenter_id.key
    SEG_CHAID = Segment.channel_id.key

    channels_df = channels_df.rename(columns={CHA_ID: SEG_CHAID})
    # get unique stations, rename Channel.id into Segment.channel_id now so we do not bother later
    stations_df = channels_df.drop_duplicates(subset=[CHA_STAID])
    stations_df.is_copy = False

    ret = []
    max_radia = get_search_radius(events_df[EVT_MAG].values, minmag, maxmag,
                                  minmag_radius, maxmag_radius)

    sourcedepths, eventtimes = [], []

    with get_progressbar(show_progress, length=len(max_radia)) as bar:
        for max_radius, evt_dic in zip(max_radia, dfrowiter(events_df, [EVT_ID, EVT_LAT, EVT_LON,
                                                                        EVT_TIME, EVT_DEPTH])):
            l2d = locations2degrees(stations_df[STA_LAT], stations_df[STA_LON],
                                    evt_dic[EVT_LAT], evt_dic[EVT_LON])
            condition = (l2d <= max_radius) & (stations_df[STA_STIME] <= evt_dic[EVT_TIME]) & \
                        (pd.isnull(stations_df[STA_ETIME]) |
                         (stations_df[STA_ETIME] >= evt_dic[EVT_TIME] + timedelta(days=1)))

            bar.update(1)
            if not np.any(condition):
                continue

            # Set (or re-set from second iteration on) as NaN SEG_EVDIST columns. This is important
            # cause from second loop on we might have some elements not-NaN which should be NaN now
            channels_df[SEG_EVDIST] = np.nan
            # set locations2 degrees
            stations_df[SEG_EVDIST] = l2d
            # Copy distances calculated on stations to their channels
            # (match along column CHA_STAID shared between the reletive dataframes). Set values
            # only for channels whose stations are within radius (stations_df[condition]):
            cha_df = mergeupdate(channels_df, stations_df[condition], [CHA_STAID], [SEG_EVDIST],
                                 drop_other_df_duplicates=False)  # dupes already dropped
            # drop channels which are not related to station within radius:
            cha_df = cha_df.dropna(subset=[SEG_EVDIST], inplace=False)
            cha_df.is_copy = False  # avoid SettingWithCopyWarning...
            cha_df[SEG_EVID] = evt_dic[EVT_ID]  # ...and add "safely" SEG_EVID values
            # append to arrays (calculate arrival times in one shot a t the end, it's faster):
            sourcedepths += [evt_dic[EVT_DEPTH]] * len(cha_df)
            eventtimes += [np.datetime64(evt_dic[EVT_TIME])] * len(cha_df)
            # Append only relevant columns:
            ret.append(cha_df[[SEG_CHAID, SEG_EVID, SEG_DCID, SEG_EVDIST]])

    # create total segments dataframe:
    # first check we have data:
    if not ret:
        raise QuitDownload(Exception(MSG("No segments to process",
                                         "No station within search radia")))
    # now concat:
    ret = pd.concat(ret, axis=0, ignore_index=True, copy=True)
    # compute travel times. Doing it on a single array is much faster
    sourcedepths = np.array(sourcedepths)
    distances = ret[SEG_EVDIST].values
    traveltimes = tttable(sourcedepths, 0, distances)
    # assign to column:
    eventtimes = np.array(eventtimes)  # should be of type  '<M8[us]' or whatever datetime dtype
    # now to compute arrival times: eventtimes + traveltimes does not work (we cannot
    # sum np.datetime64 and np.float). Convert traveltimes to np.timedelta: we first multiply by
    # 1000000 to preserve the millisecond resolution and then we write traveltimes.astype("m8[us]")
    # which means: 8bytes timedelta with microsecond resolution (10^-6)
    # Side note: that all numpy timedelta constructors (as well as "astype") round to int
    # argument, at least in numpy13.
    ret[SEG_ATIME] = eventtimes + (traveltimes*1000000).astype("m8[us]")
    # drop nat values
    oldlen = len(ret)
    ret.dropna(subset=[SEG_ATIME], inplace=True)
    if oldlen > len(ret):
        logger.info(MSG("%d of %d segments discarded", "Travel times NaN"),
                    oldlen-len(ret), oldlen)
        if not len(ret):
            raise QuitDownload(Exception(MSG("No segments to process", "All travel times NaN")))
    return ret


def prepare_for_download(session, segments_df, timespan, retry_seg_not_found, retry_url_err,
                         retry_mseed_err, retry_client_err, retry_server_err, retry_timespan_err,
                         retry_timespan_warn=False):
    """
        Drops the segments which are already present on the database and updates the primary
        keys for those not present (adding them to the db).
        Adds three new columns to the returned Data frame:
        `Segment.id` and `Segment.download_status_code`

        :param session: the sql-alchemy session bound to an existing database
        :param segments_df: pandas DataFrame resulting from `get_arrivaltimes`
    """
    # For convenience and readability, define once the mapped column names representing the
    # dataframe columns that we need:
    SEG_EVID = Segment.event_id.key
    SEG_ATIME = Segment.arrival_time.key
    SEG_START = Segment.request_start.key
    SEG_END = Segment.request_end.key
    SEG_CHID = Segment.channel_id.key
    SEG_ID = Segment.id.key
    SEG_DSC = Segment.download_code.key
    SEG_RETRY = "__do.download__"

    URLERR_CODE, MSEEDERR_CODE, OUTTIME_ERR, OUTTIME_WARN = custom_download_codes()
    # we might use dbsync('sync', ...) which sets pkeys and updates non-existing, but then we
    # would issue a second db query to check which segments should be re-downloaded (retry).
    # As the segments table might be big (hundred of thousands of records) we want to optimize
    # db queries, thus we first "manually" set the existing pkeys with a SINGLE db query which
    # gets ALSO the status codes (whereby we know what to re-download), and AFTER we call we
    # call dbsync('syncpkeys',..) which sets the null pkeys.
    # This function is basically what dbsync('sync', ...) does with the addition that we set whcch
    # segments have to be re-downloaded, if any

    # query relevant data into data frame:
    db_seg_df = dbquery2df(session.query(Segment.id, Segment.channel_id, Segment.request_start,
                                         Segment.request_end, Segment.download_code,
                                         Segment.event_id))

    # set the boolean array telling whether we need to retry db_seg_df elements (those already
    # downloaded)
    mask = False
    if retry_seg_not_found:
        mask |= pd.isnull(db_seg_df[SEG_DSC])
    if retry_url_err:
        mask |= db_seg_df[SEG_DSC] == URLERR_CODE
    if retry_mseed_err:
        mask |= db_seg_df[SEG_DSC] == MSEEDERR_CODE
    if retry_client_err:
        mask |= db_seg_df[SEG_DSC].between(400, 499.9999, inclusive=True)
    if retry_server_err:
        mask |= db_seg_df[SEG_DSC].between(500, 599.9999, inclusive=True)
    if retry_timespan_err:
        mask |= db_seg_df[SEG_DSC] == OUTTIME_ERR
    if retry_timespan_warn:
        mask |= db_seg_df[SEG_DSC] == OUTTIME_WARN

    db_seg_df[SEG_RETRY] = mask

    # update existing dataframe. If db_seg_df we might NOT set the columns of db_seg_df not
    # in segments_df. So for safetey set them now:
    segments_df[SEG_ID] = np.nan  # coerce to valid type (should be int, however allow nans)
    segments_df[SEG_RETRY] = True  # coerce to valid type
    segments_df[SEG_START] = pd.NaT  # coerce to valid type
    segments_df[SEG_END] = pd.NaT  # coerce to valid type
    segments_df = mergeupdate(segments_df, db_seg_df, [SEG_CHID, SEG_EVID],
                              [SEG_ID, SEG_RETRY, SEG_START, SEG_END])

    # Now check time bounds: segments_df[SEG_START] and segments_df[SEG_END] are the OLD time
    # bounds, cause we just set them on segments_df from db_seg_df. Some of them might be NaT,
    # those not NaT mean the segment has already been downloaded (same (channelid, eventid))
    # Now, for those non-NaT segments, set retry=True if the OLD time bounds are different
    # than the new ones (tstart, tend).
    td0, td1 = timedelta(minutes=timespan[0]), timedelta(minutes=timespan[1])
    tstart, tend = (segments_df[SEG_ATIME] - td0).dt.round('s'), \
        (segments_df[SEG_ATIME] + td1).dt.round('s')
    retry_requests_timebounds = pd.notnull(segments_df[SEG_START]) & \
        ((segments_df[SEG_START] != tstart) | (segments_df[SEG_END] != tend))
    request_timebounds_need_update = retry_requests_timebounds.any()
    if request_timebounds_need_update:
        segments_df[SEG_RETRY] |= retry_requests_timebounds
    # retry column updated: clear old time bounds and set new ones just calculated:
    segments_df[SEG_START] = tstart
    segments_df[SEG_END] = tend

    oldlen = len(segments_df)
    # do a copy to avoid SettingWithCopyWarning. Moreover, copy should re-allocate contiguous
    # arrays which might be faster (and less memory consuming after unused memory is released)
    segments_df = segments_df[segments_df[SEG_RETRY]].copy()
    if oldlen != len(segments_df):
        reason = "already downloaded, no retry"
        logger.info(MSG("%d segments discarded", reason), oldlen-len(segments_df))

    if empty(segments_df):
        raise QuitDownload("Nothing to download: all segments already downloaded according to "
                           "the current configuration")

    # warn the user if we have duplicated segments, i.e. segments of the same
    # (channel_id, request_start, request_end). This can happen when we have to very close
    # events. Note that the time bounds are given by the combinations of
    # [event.lat, event.lon, event.depth_km, segment.event_distance_deg] so the condition
    # 'duplicated segments' might actually happen
    seg_dupes_mask = segments_df.duplicated(subset=[SEG_CHID, SEG_START, SEG_END], keep=False)
    if seg_dupes_mask.any():
        seg_dupes = segments_df[seg_dupes_mask]
        logger.info(MSG("%d suspicious duplicated segments found:\n"
                        "any of these segment has by definition at least another segment\n"
                        "with the same channel_id, request_start and request_end.\n"
                        "Cause: two or more events with different id's arriving to the same\n"
                        "channel at the same date and time (rounded to the nearest second).\n"
                        "(all these segments will anyway be written to the database)."),
                    len(seg_dupes))
        logwarn_dataframe(seg_dupes.sort_values(by=[SEG_CHID, SEG_START, SEG_END]),
                          "Suspicious duplicated segments",
                          [SEG_CHID, SEG_START, SEG_END, SEG_EVID],
                          max_row_count=100)

    segments_df.drop([SEG_RETRY], axis=1, inplace=True)
    # return python bool, not numpy bool: use .item():
    return segments_df, request_timebounds_need_update.item()


def get_seg_request(segments_df, datacenter_url, chaid2mseedid_dict):
    """returns a Request object from the given segments_df"""
    # For convenience and readability, define once the mapped column names representing the
    # dataframe columns that we need:
    SEG_START = Segment.request_start.key
    SEG_END = Segment.request_end.key
    CHA_ID = Segment.channel_id.key

    stime = segments_df[SEG_START].iloc[0].isoformat()
    etime = segments_df[SEG_END].iloc[0].isoformat()

    post_data = "\n".join("{} {} {}".format(*(chaid2mseedid_dict[chaid].replace("..", ".--.").
                                              replace(".", " "), stime, etime))
                          for chaid in segments_df[CHA_ID] if chaid in chaid2mseedid_dict)
    return Request(url=datacenter_url, data=post_data.encode('utf8'))


def download_save_segments(session, segments_df, datacenters_df, chaid2mseedid_dict, download_id,
                           update_request_timebounds, max_thread_workers, timeout,
                           download_blocksize, db_bufsize, show_progress=False):

    """Downloads and saves the segments. segments_df MUST not be empty (this is not checked for)
        :param segments_df: the dataframe resulting from `prepare_for_download`
    """
    # For convenience and readability, define once the mapped column names representing the
    # dataframe columns that we need:
    SEG_CHAID = Segment.channel_id.key
    SEG_DCID = Segment.datacenter_id.key
    DC_ID = DataCenter.id.key
    DC_DSURL = DataCenter.dataselect_url.key
    SEG_ID = Segment.id.key
    SEG_START = Segment.request_start.key
    SEG_END = Segment.request_end.key
    SEG_STIME = Segment.start_time.key
    SEG_ETIME = Segment.end_time.key
    SEG_DATA = Segment.data.key
    SEG_DSCODE = Segment.download_code.key
    SEG_DATAID = Segment.data_identifier.key
    SEG_MGAP = Segment.maxgap_numsamples.key
    SEG_SRATE = Segment.sample_rate.key
    SEG_DOWNLID = Segment.download_id.key
    SEG_ATIME = Segment.arrival_time.key

    # set once the dict of column names mapped to their default values.
    # Set nan to let pandas understand it's numeric. None I don't know how it is converted
    # (should be checked) but it's for string types
    # for numpy types, see
    # https://docs.scipy.org/doc/numpy/reference/arrays.dtypes.html#specifying-and-constructing-data-types
    # Use OrderedDict to preserve order (see comments below)
    segvals = OrderedDict([(SEG_DATA, None), (SEG_SRATE, np.nan), (SEG_MGAP, np.nan),
                           (SEG_DATAID, None), (SEG_DSCODE, np.nan), (SEG_STIME, pd.NaT),
                           (SEG_ETIME, pd.NaT)])
    # Define separate keys cause we will use it elsewhere:
    # Note that the order of these keys must match `mseed_unpack` returned data
    # (this is why we used OrderedDict above)
    SEG_COLNAMES = list(segvals.keys())
    # define default error codes:
    URLERR_CODE, MSEEDERR_CODE, OUTTIME_ERR, OUTTIME_WARN = custom_download_codes()
    SEG_NOT_FOUND = None

    stats = DownloadStats()

    datcen_id2url = datacenters_df.set_index([DC_ID])[DC_DSURL].to_dict()

    colnames2update = [SEG_DOWNLID, SEG_DATA, SEG_SRATE, SEG_MGAP, SEG_DATAID, SEG_DSCODE,
                       SEG_STIME, SEG_ETIME]
    if update_request_timebounds:
        colnames2update += [SEG_START, SEG_ATIME, SEG_END]

    cols_to_log_on_err = [SEG_ID, SEG_CHAID, SEG_START, SEG_END, SEG_DCID]
    segmanager = DbManager(session, Segment.id, colnames2update,
                           db_bufsize, return_df=False,
                           oninsert_err_callback=handledbexc(cols_to_log_on_err, update=False),
                           onupdate_err_callback=handledbexc(cols_to_log_on_err, update=True))

    # define the groupsby columns
    # remember that segments_df has columns:
    # ['channel_id', 'datacenter_id', 'event_distance_deg', 'event_id', 'arrival_time',
    #  'request_start', 'request_end', 'id']
    # first try to download per-datacenter and time bounds. On 413, load each
    # segment separately (thus use SEG_DCID_NAME, SEG_SART_NAME, SEG_END_NAME, SEG_CHAID_NAME
    # (and SEG_EVTID_NAME for safety?)

    # we should group by (net, sta, loc, stime, etime), meaning that two rows with those values
    # equal will be given in the same sub-dataframe, and if 413 is found, take 413s erros creating a
    # new dataframe, and then group segment by segment, i.e.
    # (net, sta, loc, cha, stime, etime).
    # Unfortunately, for perf reasons we do not have
    # the first 4 columns, but we do have channel_id which basically comprises (net, sta, loc, cha)
    # NOTE: SEG_START and SEG_END MUST BE ALWAYS PRESENT IN THE SECOND AND THORD POSITION!!!!!
    requeststart_index = 1
    requestend_index = 2
    groupsby = [
                [SEG_DCID, SEG_START, SEG_END],
                [SEG_DCID, SEG_START, SEG_END, SEG_CHAID],
                ]

    if sys.version_info[0] < 3:
        def get_host(r):
            return r.get_host()
    else:
        def get_host(r):
            return r.host

    # we assume it's the terminal, thus allocate the current process to track
    # memory overflows
    with get_progressbar(show_progress, length=len(segments_df)) as bar:

        skipped_dataframes = []  # store dataframes with a 413 error and retry later
        for group_ in groupsby:

            if segments_df.empty:  # for safety (if this is the second loop or greater)
                break

            islast = group_ == groupsby[-1]
            seg_groups = segments_df.groupby(group_, sort=False)
            # seg group is an iterable of 2 element tuples. The first element is the tuple
            # of keys[:idx] values, and the second element is the dataframe
            itr = read_async(seg_groups,
                             urlkey=lambda obj: get_seg_request(obj[1], datcen_id2url[obj[0][0]],
                                                                chaid2mseedid_dict),
                             raise_http_err=False,
                             max_workers=max_thread_workers,
                             timeout=timeout, blocksize=download_blocksize)

            for df, result, exc, request in itr:
                groupkeys_tuple = df[0]
                df = df[1]  # copy data so that we do not have refs to the old dataframe
                # and hopefully the gc works better
                url = get_host(request)
                data, code, msg = result if not exc else (None, None, None)
                if code == 413 and len(df) > 1 and not islast:
                    skipped_dataframes.append(df)
                    continue
                # Seems that copy(), although allocates a new small memory chunk,
                # helps gc better managing total memory (which might be an issue):
                df = df.copy()
                # init columns with default values:
                for col in SEG_COLNAMES:
                    df[col] = segvals[col]
                    # Note that we could use
                    # df.insert(len(df.columns), col, segvals[col])
                    # to preserve order, if needed. A starting discussion on adding new column:
                    # https://stackoverflow.com/questions/12555323/adding-new-column-to-existing-dataframe-in-python-pandas
                # init download id column with our download_id:
                df[SEG_DOWNLID] = download_id
                if exc:
                    code = URLERR_CODE
                elif code >= 400:
                    exc = "%d: %s" % (code, msg)
                elif not data:
                    # if we have empty data set only specific columns:
                    # (avoid mseed_id as is useless string data on the db, and we can retrieve it
                    # via station and channel joins in case)
                    df.loc[:, SEG_DATA] = b''
                    df.loc[:, SEG_DSCODE] = code
                    stats[url][code] += len(df)
                else:
                    try:
                        starttime = groupkeys_tuple[requeststart_index]
                        endtime = groupkeys_tuple[requestend_index]
                        resdict = mseedunpack(data, starttime, endtime)
                        oks = 0
                        errors = 0
                        outtime_warns = 0
                        outtime_errs = 0
                        # iterate over df rows and assign the relative data
                        # Note that we could use iloc which is SLIGHTLY faster than
                        # loc for setting the data, but this would mean using column
                        # indexes and we have column labels. A conversion is possible but
                        # would make the code  hard to understand (even more ;))
                        for idxval, chaid in zip(df.index.values, df[SEG_CHAID]):
                            mseedid = chaid2mseedid_dict.get(chaid, None)
                            if mseedid is None:
                                continue
                            # get result:
                            res = resdict.get(mseedid, None)
                            if res is None:
                                continue
                            err, data, s_rate, max_gap_ratio, stime, etime, outoftime = res
                            if err is not None:
                                # set only the code field.
                                # Use set_value as it's faster for single elements
                                df.set_value(idxval, SEG_DSCODE, MSEEDERR_CODE)
                                stats[url][MSEEDERR_CODE] += 1
                                errors += 1
                            else:
                                if outoftime is True:
                                    if data:
                                        code = OUTTIME_WARN
                                        outtime_warns += 1
                                    else:
                                        code = OUTTIME_ERR
                                        outtime_errs += 1
                                else:
                                    oks += 1
                                # This raises a UnicodeDecodeError:
                                # df.loc[idxval, SEG_COLNAMES] = (data, s_rate,
                                #                                 max_gap_ratio,
                                #                                 mseedid, code)
                                # The problem (bug?) is in pandas.core.indexing.py
                                # on line 517: np.array((data, s_rate, max_gap_ratio,
                                #                                  mseedid, code))
                                # (numpy coerces to unicode if one of the values is unicode,
                                #  and thus fails for the `data` field?)
                                # Anyway, we set first an empty string (which can be
                                # decoded) and then use set_value only for the `data` field
                                # set_value should be relatively fast
                                df.loc[idxval, SEG_COLNAMES] = (b'', s_rate, max_gap_ratio,
                                                                mseedid, code, stime, etime)
                                df.set_value(idxval, SEG_DATA, data)

                        if oks:
                            stats[url][code] += oks
                        if outtime_errs:
                            stats[url][code] += outtime_errs
                        if outtime_warns:
                            stats[url][code] += outtime_warns

                        unknowns = len(df) - oks - errors - outtime_errs - outtime_warns
                        if unknowns > 0:
                            stats[url][SEG_NOT_FOUND] += unknowns
                    except MSeedError as mseedexc:
                        code = MSEEDERR_CODE
                        exc = mseedexc
#                     except Exception as unknown_exc:
#                         code = None
#                         exc = unknown_exc

                if exc is not None:
                    df.loc[:, SEG_DSCODE] = code
                    stats[url][code] += len(df)
                    logger.warning(MSG("Unable to get waveform data", exc, request))

                segmanager.add(df)
                bar.update(len(df))

            segmanager.flush()  # flush remaining stuff to insert / update, if any

            if skipped_dataframes:
                segments_df = pd.concat(skipped_dataframes, axis=0, ignore_index=True, copy=True,
                                        verify_integrity=False)
                skipped_dataframes = []
            else:
                # break the next loop, if any
                segments_df = pd.DataFrame()

    segmanager.close()  # flush remaining stuff to insert / update, if any, and prints info

    stats.normalizecodes()  # this makes potential string code merge into int codes
    return stats


def _get_sta_request(datacenter_url, network, station, start_time, end_time):
    """
    returns a Request object from the given station arguments to download the inventory xml"""
    # we need a endtime (ingv does not accept * as last param)
    # note :pd.isnull(None) is true, as well as pd.isnull(float('nan')) and so on
    et = datetime.utcnow().isoformat() if pd.isnull(end_time) else end_time.isoformat()
    post_data = " ".join("*" if not x else x for x in[network, station, "*", "*",
                                                      start_time.isoformat(), et])
    return Request(url=datacenter_url, data="level=response\n{}".format(post_data).encode('utf8'))


def save_inventories(session, stations_df, max_thread_workers, timeout,
                     download_blocksize, db_bufsize, show_progress=False):
    """Save inventories. Stations_df must not be empty (this is not checked for)"""

    _msg = "Unable to save inventory (station id=%d)"

    downloaded, errors, empty = 0, 0, 0
    cols_to_log_on_err = [Station.id.key, Station.network.key, Station.station.key,
                          Station.start_time.key]
    dbmanager = DbManager(session, Station.id,
                          update=[Station.inventory_xml.key],
                          buf_size=db_bufsize,
                          oninsert_err_callback=handledbexc(cols_to_log_on_err, update=False),
                          onupdate_err_callback=handledbexc(cols_to_log_on_err, update=True))

    with get_progressbar(show_progress, length=len(stations_df)) as bar:
        iterable = zip(stations_df[Station.id.key],
                       stations_df[DataCenter.station_url.key],
                       stations_df[Station.network.key],
                       stations_df[Station.station.key],
                       stations_df[Station.start_time.key],
                       stations_df[Station.end_time.key])
        for obj, result, exc, request in read_async(iterable,
                                                    urlkey=lambda obj: _get_sta_request(*obj[1:]),
                                                    max_workers=max_thread_workers,
                                                    blocksize=download_blocksize, timeout=timeout,
                                                    raise_http_err=True):
            bar.update(1)
            sta_id = obj[0]
            if exc:
                logger.warning(MSG(_msg, exc, request), sta_id)
                errors += 1
            else:
                data, code, msg = result  # @UnusedVariable
                if not data:
                    empty += 1
                    logger.warning(MSG(_msg, "empty response", request), sta_id)
                else:
                    downloaded += 1
                    dbmanager.add(pd.DataFrame({Station.id.key: [sta_id],
                                                Station.inventory_xml.key: [dumps_inv(data)]}))

    logger.info(("Summary of web service responses for station inventories:\n"
                 "- downloaded     %6d \n"
                 "- discarded      %6d (empty response)\n"
                 "- not downloaded %6d (client/server errors)") %
                (downloaded, empty, errors))
    dbmanager.close()


def run(session, download_id, eventws, start, end, dataws, eventws_query_args,
        search_radius, channels, min_sample_rate, update_metadata, inventory, timespan,
        retry_seg_not_found, retry_url_err, retry_mseed_err, retry_client_err, retry_server_err,
        retry_timespan_err, traveltimes_model, advanced_settings, isterminal=False):
    """
        Downloads waveforms related to events to a specific path. FIXME: improve doc
    """
    tt_table = TTTable(get_ttable_fpath(traveltimes_model))

    # set blocksize if zero:
    if advanced_settings['download_blocksize'] <= 0:
        advanced_settings['download_blocksize'] = -1
    if advanced_settings['max_thread_workers'] <= 0:
        advanced_settings['max_thread_workers'] = None
    dbbufsize = min(advanced_settings['db_buf_size'], 1)

    process = psutil.Process(os.getpid()) if isterminal else None
    __steps = 6 + inventory  # bool substraction works: 8 - True == 7
    stepiter = iter(range(1, __steps+1))

    # custom function for logging.info different steps:
    def stepinfo(text, *args, **kwargs):
        step = next(stepiter)
        memused = ''
        if process is not None:
            percent = process.memory_percent()
            if args or kwargs:
                memused = " (%.1f%% memory used)" % percent
            else:
                memused = " (%.1f%% memory used)" % percent
        logger.info("\nSTEP %d of %d%s: {}".format(text), step, __steps, memused, *args, **kwargs)

    startiso = start.isoformat()
    endiso = end.isoformat()

    # events and datacenters should print meaningful stuff
    # cause otherwise is unclear why the program stop so quickly
    stepinfo("Requesting events")
    # eventws_url = get_eventws_url(session, service)
    try:
        events_df = get_events_df(session, eventws, dbbufsize, start=startiso, end=endiso,
                                  **eventws_query_args)
    except QuitDownload as dexc:
        return dexc.log()

    # Get datacenters, store them in the db, returns the dc instances (db rows) correctly added:
    stepinfo("Requesting data-centers")
    try:
        datacenters_df, eidavalidator = \
            get_datacenters_df(session, dataws, advanced_settings['routing_service_url'],
                               channels, start, end, dbbufsize)
    except QuitDownload as dexc:
        return dexc.log()

    stepinfo("Requesting stations and channels from %d %s", len(datacenters_df),
             "data-center" if len(datacenters_df) == 1 else "data-centers")
    try:
        channels_df = get_channels_df(session, datacenters_df, eidavalidator,
                                      channels, start, end,
                                      min_sample_rate, update_metadata,
                                      advanced_settings['max_thread_workers'],
                                      advanced_settings['s_timeout'],
                                      advanced_settings['download_blocksize'], dbbufsize,
                                      isterminal)
    except QuitDownload as dexc:
        return dexc.log()

    # get channel id to mseed id dict and purge channels_df
    # the dict will be used to download the segments later, but we use it now to drop
    # unnecessary columns and save space (and time)
    chaid2mseedid = chaid2mseedid_dict(channels_df, drop_mseedid_columns=True)

    stepinfo("Selecting stations within search area from %d events", len(events_df))
    try:
        segments_df = merge_events_stations(events_df, channels_df, search_radius['minmag'],
                                            search_radius['maxmag'], search_radius['minmag_radius'],
                                            search_radius['maxmag_radius'], tt_table, isterminal)
    except QuitDownload as dexc:
        return dexc.log()

    # help gc by deleting the (only) refs to unused dataframes
    del events_df
    del channels_df

    stepinfo("%d segments found. Checking already downloaded segments", len(segments_df))
    exit_code = 0
    try:
        segments_df, request_timebounds_need_update = \
            prepare_for_download(session, segments_df, timespan, retry_seg_not_found,
                                 retry_url_err, retry_mseed_err, retry_client_err,
                                 retry_server_err, retry_timespan_err,
                                 retry_timespan_warn=False)

        # download_save_segments raises a QuitDownload if there is no data, so if we are here
        # segments_df is not empty
        stepinfo("Downloading %d segments and saving to db", len(segments_df))

        # frees memory. Although maybe unecessary, let's do our best to free stuff cause the
        # next one is memory consuming:
        # https://stackoverflow.com/questions/30021923/how-to-delete-a-sqlalchemy-mapped-object-from-memory
        session.expunge_all()
        session.close()

        d_stats = download_save_segments(session, segments_df, datacenters_df,
                                         chaid2mseedid, download_id,
                                         request_timebounds_need_update,
                                         advanced_settings['max_thread_workers'],
                                         advanced_settings['w_timeout'],
                                         advanced_settings['download_blocksize'],
                                         dbbufsize,
                                         isterminal)
        del segments_df  # help gc?
        session.close()  # frees memory?
        logger.info("")
        logger.info(("** Segments download summary **\n"
                     "Number of segments per data-center (rows) and response "
                     "status (columns):\n%s") %
                    str(d_stats) or "Nothing to show")

    except QuitDownload as dexc:
        # we are here if:
        # 1) we didn't have segments in prepare_for... (QuitDownload with string message)
        # 2) we ran out of memory in download_... (QuitDownload with exception message

        # in the first case continue, in the latter return a nonzero exit code
        exit_code = dexc.log()
        if exit_code != 0:
            return exit_code

    if inventory:
        # frees memory. Although maybe unecessary, let's do our best to free stuff cause the
        # next one might be memory consuming:
        # https://stackoverflow.com/questions/30021923/how-to-delete-a-sqlalchemy-mapped-object-from-memory
        session.expunge_all()
        session.close()

        # query station id, network station, datacenter_url
        # for those stations with empty inventory_xml
        # AND at least one segment non empty/null
        # Download inventories for those stations only
        sta_df = dbquery2df(query4inventorydownload(session, update_metadata))
        # stations = session.query(Station).filter(~withdata(Station.inventory_xml)).all()
        if empty(sta_df):
            stepinfo("Skipping: No station inventory to download")
        else:
            stepinfo("Downloading %d station inventories", len(sta_df))
            save_inventories(session, sta_df,
                             advanced_settings['max_thread_workers'],
                             advanced_settings['i_timeout'],
                             advanced_settings['download_blocksize'], dbbufsize, isterminal)

    return exit_code

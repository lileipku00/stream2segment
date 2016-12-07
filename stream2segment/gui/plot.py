'''
Program to annotate classes of mseeds previously downloaded with this program
Created on Feb 25, 2016

@author: riccardo
'''
# import matplotlib
# matplotlib.use('Qt4Agg')
import numpy as np
from datetime import timedelta
import sys
from stream2segment.io.db import ListReader as ClassAnnotator
import matplotlib.pyplot as plt
from matplotlib.widgets import RadioButtons
# Overriding default buttons behaviour:
from matplotlib.backend_bases import NavigationToolbar2
from scipy.signal import savgol_filter
from obspy.signal.konnoohmachismoothing import konno_ohmachi_smoothing as kos
# set here left or right:
plot_position = 'right'

# Rewrite tooltiptexts for back and forward buttons (we copy the whole tuple defined in
# NavigatorToolbar2 although it's quite inefficient because it's easier than modifying a tuple of
# tuples
NavigationToolbar2.toolitems = toolitems = (
        ('Home', 'Reset original view', 'home', 'home'),
        ('Back', 'Back to  previous plot', 'back', 'back'),
        ('Forward', 'Forward to next plot', 'forward', 'forward'),
        (None, None, None, None),
        ('Pan', 'Pan axes with left mouse, zoom with right', 'move', 'pan'),
        ('Zoom', 'Zoom to rectangle', 'zoom_to_rect', 'zoom'),
        (None, None, None, None),
        ('Subplots', 'Configure subplots', 'subplots', 'configure_subplots'),
        ('Save', 'Save the figure', 'filesave', 'save_figure'),
      )
# titems_lst = list(NavigationToolbar2.toolitems)
# titems_lst.insert(4, ('Home', 'Reset original view', 'stock_refresh', 'home'))
# NavigationToolbar2.toolitems = tuple(titems_lst)

# NavigationToolbar2.home = new_home
NavigationToolbar2.back = lambda self, *args, **kwargs: plot_other(self, -1)
NavigationToolbar2.forward = lambda self, *args, **kwargs: plot_other(self, 1)

# global vars:
curr_pos = 0
fig = plt.figure(figsize=(16, 9), dpi=80)
class_annotator = None
# keep a reference to the figure title so that we do not need to create one every time
# (avoiding checking for titles etcetera)
infotext = fig.suptitle("", multialignment='left', fontsize=13, family='monospace',
                        horizontalalignment='left', verticalalignment='top')
# http://matplotlib.org/users/text_props.html:
# horizontalalignment controls whether the x positional argument for the text indicates the left,
# center or right side of the text bounding box. verticalalignment controls whether the y
# positional argument for the text indicates the bottom, center or top side of the
# text bounding box. multialignment, for newline separated strings only, controls whether
# the different lines are left, center or right justified

# some global variables for axes and controls dimensions:
# padding around figure. This is the padding of the shorter axes, usually the y one
# (the other one will be adjusted accordingly)
fig_padding = 0.025
legend_width = 0.35

# ids shown (empty means show all):
shown_filters = []

# these variables are global so that we can make them interactive
# the axes housing the radiobuttons:
# Note: the position (0.95, 0.3) will be RESET later, here only elements 3 and 4 (width and height)
# are set!
rax = plt.axes([0.95, 0.3, legend_width, legend_width*0.75],  # axisbg='lightgoldenrodyellow',
               title="Class ID",
               aspect='equal')  # the last one makes radio buttons circles and not ellipses
# the radiobuttons widget:
radiobuttons = None

_pass_set_flag = False  # this flag is set in updateradiobuttons and used in setclass


def setclass(label):
    if _pass_set_flag:
        return
    idx = -1
    for idx, txt in enumerate(radiobuttons.labels):
        # inefficient but it is independent of label caption
        if txt.get_text() == label:
            classes_df = class_annotator.get_classes_df()
            class_id = classes_df.iloc[idx]['Id']
            class_annotator.set_class(curr_pos, class_id)
            update_radio_buttons()


def plot_other(self, key=0):  # key = None: home (print first plot), +1: print next, -1: print prev.
    global curr_pos
    old_curr_pos = curr_pos
    curr_pos = 0 if key is None else (curr_pos + key) % len(class_annotator)
    if old_curr_pos != curr_pos:
        plot(self.canvas, curr_pos)


def mseed_axes_iterator(fig):
    """returns all axes within the figure which are mseed plots (assuming they are all BUT rax)"""
    axes_ = fig.get_axes()
    for a in axes_:
        if a != rax:
            yield a


def getinfotext(metadata_list):
    """Returns a nicely formatted string from the mseed metadata read from db"""
    first_col_chars = max(len(str(key[0])) for key in metadata_list)
    max_second_col_chars = 42

    # custom str function replacement for the dict values:
    def ztr(data):
        val = str(data)
        if len(val) <= max_second_col_chars:
            return val
        splits = [(i * max_second_col_chars, (i+1) * max_second_col_chars)
                  for i in xrange(len(val)/(max_second_col_chars))]
        if len(val) % max_second_col_chars > 0:
            splits.append((splits[-1][1], None))
        return ("\n " + (" " * first_col_chars)).join(val[s[0]:s[1]] for s in splits)

    # print the metadata on the figure title. Set the format string:
    frmt_str = "{0:" + str(first_col_chars) + "} {1}"
    title_str = "\n".join(frmt_str.format(str(k[0]), ztr(k[1])) for k in metadata_list)
    return title_str


def filter_(data, freq_min=0.1, freq_max=20, corners=2):
    """filters a signal data as obtained from obspy.read"""
    data_filtered = data.copy()

    # define sampling freq
    sampling_rate = data[0].stats.sampling_rate
    # adjust the max_f_max to 0.9 of the nyquist frea (sampling rate /2)
    max_f_max = 0.9 * (sampling_rate / 2.0)

    freq_max = min(freq_max, max_f_max)

    # pad data with zeros at the beginnign in order to accomodate the transient
#     lengths = []
#     for d in data_filtered:
#         lengths.append(len(d.data))
#         d.data = np.lib.pad(d.data, (0, lengths[-1]), 'constant', constant_values=(0, 0))

    data_filtered = data_filtered.filter('bandpass', freqmin=freq_min, freqmax=freq_max,
                                         corners=corners, zerophase=True)

#     for l, d in zip(lengths, data_filtered):
#         d.data = d.data[l:]

    # smooth tail artifacts due to transients:
    data_filtered.taper(type='cosine', max_percentage=0.05)

    return data_filtered

def todt(obj):
    try:
        return obj.to_datetime()
    except AttributeError:
        try:
            return obj.datetime
        except AttributeError:    
            return obj.datetime
    
    return obj

def get_noise_spectra(trace, fixed_time, window_in_sec):
    fixed_time = todt(fixed_time)

    noise_dt = [fixed_time - timedelta(seconds=window_in_sec), fixed_time]
    normal_dt = [fixed_time, fixed_time + timedelta(seconds=window_in_sec)]

    
    start = todt(trace.stats.starttime)
    end = todt(trace.stats.endtime)

    if start > noise_dt[0]:
        noise_dt[0] = start

    if end < noise_dt[1]:
        noise_dt[1] = end

    dt = trace.stats.delta

    noise_idxs = [
                  int((noise_dt[0] - start).total_seconds() / dt),
                  int((noise_dt[1] - start).total_seconds() / dt)
                  ]

    normal_idxs = [
                  int((normal_dt[0] - start).total_seconds() / dt),
                  int((normal_dt[1] - start).total_seconds() / dt)
                  ]

    signal1 = trace.data[noise_idxs[0]: noise_idxs[1]]
    signal2 = trace[normal_idxs[0]: normal_idxs[1]]

    ft1 = np.fft.rfft(signal1)
    freq1 = np.fft.rfftfreq(len(signal1), d=dt)
    ft2 = np.fft.rfft(signal2)
    freq2 = np.fft.rfftfreq(len(signal2), d=dt)

    return [
            {'freqs': freq1, 'vals': ft1},
            {'freqs': freq2, 'vals': ft2},
            ]
    
    

#     signal1=tmp[in1:in2]
#     n1=len(signal1)
#     signal2=tmp[is1:is2]
#     n2=len(signal2)
#     q=[n1,n2]
#     print(q)
#     ft1=np.fft.rfft(signal1)
#     freq1 = np.fft.rfftfreq(n1, d=dt)
#     ft2=np.fft.rfft(signal2)
#     freq2 = np.fft.rfftfreq(n2, d=dt)
#     mtf1=np.abs(ft1)
#     mtf2=np.abs(ft2)
#     smtf1=savitzky_golay(mtf1, 101, 3)   #smoothing
#     smtf2=savitzky_golay(mtf2, 101, 3)

    
def plot(canvas, index):

    canvas.set_window_title("%s: FILE %d OF %d" % (class_annotator.db_uri,
                                                   index+1,
                                                   len(class_annotator)))
    data = None
    # canvas.figure.clear() this is BAD cause the radiobuttons do not work anymore. Then
    # clear only axes of interest:
    for a in mseed_axes_iterator(fig):
        if a != rax:
            fig.delaxes(a)

    # mdt = class_annotator.get(index).iloc[0]['Data']
    # mdt = class_annotator.read(index)
    other_components_data = None  # we need to separate the other components as we CANNOT
    # retrieve which is the current plotted data from obspy plot

    try:
        segment_data = class_annotator.get(class_annotator.get_id(index),
                                            class_annotator.T_SEG)
        segment_series = segment_data.iloc[0]
        data = class_annotator.mseed(segment_series)

        def filter_func(df):
            return df[(df['#Network'] == segment_series['#Network']) &
                      (df['Station'] == segment_series['Station']) &
                      (df['Location'] == segment_series['Location']) &
                      (df['DataStartTime'] == segment_series['DataStartTime']) &
                      (df['DataEndTime'] == segment_series['DataEndTime']) &
                      (df['Channel'].str[:2] == segment_series['Channel'][:2]) &
                      (df['Channel'] != segment_series['Channel'])]

        other_components = class_annotator.read(class_annotator.T_SEG, filter_func=filter_func)

#  # optionally also:
#         tseg = class_annotator.T_SEG
#         col = class_annotator.column
#         where = and_(col(tseg, "#Network") == segment_series['#Network'],
#                      col(tseg, "Station") == segment_series['Station'],
#                      col(tseg, "Location") == segment_series['Location'],
#                      col(tseg, 'DataStartTime') == segment_series['DataStartTime'],
#                      col(tseg, 'DataEndTime') == segment_series['DataEndTime'])  #,
#                      # col(tseg, 'Channel')[:2] == sss['Channel'][:2])
#         other_components = class_annotator.select([tseg], where)

        for _, row in other_components.iterrows():
            if other_components_data is None:
                other_components_data = class_annotator.mseed(row)
            else:
                dta = class_annotator.mseed(row)
                other_components_data.traces.append(dta.traces[0])

        # apply filter
        # filtered_data = data.filter('highpass', freq=0.1, corners=2, zerophase=False)
        # data.traces.append(filtered_data.traces[0])
        # data = filtered_data

    except (IOError, TypeError) as ioerr:
        # canvas.figure.suptitle(str(ioerr))
        errmsg = "Unable to show data plot(s):\n%s: %s" % (str(ioerr.__class__.__name__), str(ioerr))
        infotext.set_text(errmsg)
        # canvas.draw()
        return

    # preprocess the signals
    data = filter_(data)
    other_components_data = filter_(other_components_data)
    
    # PLot SNR:
#     arrival_time = segment_data[class_annotator.T_SEG]['ArrivalTime']
#     ret = get_noise_spectra(data[0], arrival_time, 40)
#     
#     ax = fig.add_axes([0, 0, 1, 1])
#     
#     signals = np.array([np.abs(ret[0]['vals']), np.abs(ret[1]['vals'])])
#     freqs = np.array([ret[0]['freqs'], ret[1]['freqs']])
#     ys2 = kos(signals, ret[0]['freqs'])
#     
#     ax.loglog(ret[0]['freqs'], ys2[0], color='g', lw=3)
#
#     ax.loglog(ret[1]['freqs'], ys2[1], color='r', lw=3)
    
    # FIXME: add sum of squares and plot the ratio S/N
    
    # ============================
    
    # PLOT CUMULATIVE
    
#     yval = data[0].data
#     cumsum = np.cumsum(yval**2, axis=None, dtype=None, out=None)
#     cumsum /= np.max(cumsum)
#     ax = fig.add_axes([0, 0, 1, 1])
#     npt = len(yval)
#     time = (np.linspace(1, npt, npt)) * data[0].stats.delta
#     ax.plot(time, cumsum, color='k', lw=3)

    # =============================

    # PLOT ENVELOPE

    # get spectrogram
    from scipy.signal import spectrogram
    f, t, sxx = spectrogram(data[0].data, data[0].stats.sampling_rate)
    
    fmin = 2
    fmax = 10
    tmin=-100000
    tmax=100000
    ffilt = [i for i in list(range(len(f))) if (f[i] >= fmin) and (f[i] <= fmax)]
    indxt = [i for i in list(range(len(t))) if (t[i]>=tmin) and (t[i]<=tmax)]
    ener = np.zeros(len(indxt))
    ii = 0
    for i in xrange(len(ener)):
        dum = np.abs(sxx[ffilt,i])#**2  #either norm 1 or norm 2
        ener[ii] = dum.sum()
        ii= ii + 1 

    enerdb = 10*np.log10(ener)
    
    ax = fig.add_axes([0, 0, 1, 1])
    ax.plot(enerdb)
    
    
    # =========================================
    
    data.plot(fig=fig, draw=False)  # , block=True)

    xlim = None  # drawback: by adding other_components_data we can know which is the currently
    # selected plot (i.e., the ones in the 'data' variable) BUT axis align is messed up. Do it here:
    def_axez = []
    for axs in mseed_axes_iterator(fig):
        xlim = axs.get_xlim()
        def_axez.append(axs)

    if other_components_data is not None:
        other_components_data.plot(fig=fig, color='#cccccc', draw=False)
        if xlim:
            for axs in mseed_axes_iterator(fig):
                if axs not in def_axez:
                    axs.set_xlim(xlim)
                    axs.set_xticklabels([])

    axez = sorted(mseed_axes_iterator(fig), key=lambda ax: ax.get_position().y0)

    # calculate fig padding:
    # NOTE: fig_padding does not include axis ticks. For the vertical ones is ok, as they are
    # a single line height, for the horizontal one we add a bit more space:
    fig_padding_h, fig_padding_w = fig_padding, fig_padding
    sizez = fig.get_size_inches()
    if sizez[0] > sizez[1]:
        fig_padding_w *= sizez[1] / sizez[0]
    elif sizez[1] > sizez[0]:
        fig_padding_h *= sizez[0] / sizez[1]

    ypos = fig_padding_h
    additional_left_margin = 0.03
    height = (1.0 - 2*(fig_padding_h)) / len(axez)
    width = (1.0 - 3*(fig_padding_w) - additional_left_margin - legend_width)
    axez_x = fig_padding_w + additional_left_margin if plot_position == 'left' else \
        legend_width + 2*fig_padding_w + additional_left_margin
    for axs in axez:
        # testing: do we really set the ypos on the right axes?
        # print str(axs.get_position().y0) + " " + str(ypos)
        axs.set_position([axez_x, ypos, width, height])
        ypos += height

    # Set info text on the figure title (NOTE: it is placed on the right)

    infodata = []  # preserve order
    for k in ("#EventID", "EventDistance/deg", "Magnitude", "", "DataStartTime", "ArrivalTime",
              "DataEndTime", "", "#Network", "Station", "Location", "Channel", "", "RunId"):
        value = "" if not k else \
            segment_series[k] if k in segment_series else ""  # segment_data[class_annotator.T_EVT][k]
        infodata.append((k, value))
    infotext.set_text(getinfotext(infodata))

    # adjust dimensions:
    xxx = 1 - legend_width - fig_padding_w if plot_position == 'left' else \
        fig_padding_w
    # infotext:
    infotext.set_position((xxx, 1-fig_padding_h))
    # set radiobuttons position:
    rax_pos = rax.get_position()
    rax.set_position([xxx, fig_padding_h, legend_width, rax_pos.height])
    # update the selected radio button
    update_radio_buttons(update_texts=False)


def update_radio_buttons(update_texts=True):
    """
        Updates the radio buttons
        :param update_texts: updates the label texts (with the counts for each class label)
        :param update_active: if True, sets the active button according to the selected mseed class
        SET TO FALSE IF CALLING THIS FROM WITHIN A MOUSE CLICK ON ONE RADIO BUTTON TO AVOID
        INFINITE LOOPS
    """
    pass
#     global radiobuttons, class_annotator
#     classes_df = class_annotator.get_classes_df()
# 
#     if update_texts or radiobuttons is None:
#         ids = classes_df['Id'].tolist()
#         clbls = classes_df['Label'].tolist()
#         counts = classes_df['Count'].tolist()
#         radiolabels = ["%d: %s (%d)" % (i, s, v) for i, s, v in zip(ids, clbls, counts)]
#         if radiobuttons is None:
#             radiobuttons = RadioButtons(rax, radiolabels)
#             if len(shown_filters):
#                 for i, text in enumerate(radiobuttons.labels):
#                     if ids[i] not in shown_filters:
#                         text.set_color('#bbbbbb')
#             # Resize all radio buttons in `r` collection by fractions `f`"
#             for circle in radiobuttons.circles:
#                 circle.set_radius(circle.get_radius() * .75)
# 
#             radiobuttons.on_clicked(setclass)  # set this after 'update_radio_buttons' above
#         else:
#             for text, label in zip(radiobuttons.labels, radiolabels):
#                 text.set_text(label)
# 
#     global _pass_set_flag
#     _pass_set_flag = True
#     class_id = class_annotator.get_class(class_annotator.get_id(curr_pos))
#     radiobuttonindex = classes_df[classes_df['Id'] == class_id].index[0]
#     radiobuttons.set_active(radiobuttonindex)
#     _pass_set_flag = False


def main(db_uri, class_ids):
    global class_annotator
    global shown_filters

    if class_ids:
        shown_filters = class_ids

        def filter_func(dframe):
            return dframe[dframe['AnnotatedClassId'].isin(class_ids)]

    class_annotator = ClassAnnotator(db_uri, filter_func=None if not class_ids else filter_func,
                            sort_columns=["#EventID", "EventDistance/deg"], sort_ascending=[True,
                                                                                            True])
    plot(fig.canvas, 0)
    plt.show(True)


if __name__ == '__main__':
    # global files
    if len(sys.argv) < 2:
        print "please specify a valid directory of mseed files"
        sys.exit(1)

    path_ = sys.argv[1]
    # files = [join(dir_, f) for f in listdir(dir_) if isfile(join(dir_, f))]
        # plt.show(block=True)
    
    main(path_, [])


# SOME INFOS FOUND BROWSING INTERNET (SEOM OF THEM USED ABOVE):

# A) THIS HIDES THE TOOLBAR:
# import matplotlib as mpl
# mpl.rcParams['toolbar'] = 'None'

# B) THIS OVERRIDES THE DEFAULT BEHAVIOR:
# See http://stackoverflow.com/questions/14896580/matplotlib-hooking-in-to-home-back-forward-button-events
# from matplotlib.backend_bases import NavigationToolbar2
# home = NavigationToolbar2.home
# def new_home(self, *args, **kwargs):
#     print 'new home'
#     home(self, *args, **kwargs)
# NavigationToolbar2.home = new_home

# C) THIS OVERRIDES THE MOUSE / KEY EVENTS ON THE PLOT (NOT THE NAVIGATION TOOLBAR):
# See http://matplotlib.org/users/event_handling.html
# def onclick(event):
#     print 'button=%d, x=%d, y=%d, xdata=%f, ydata=%f'%(
#         event.button, event.x, event.y, event.xdata, event.ydata)
# cid = fig.canvas.mpl_connect('button_press_event', onclick)

    # To set the title of the axes withion a figure:
    # self.canvas.figure.suptitle('ah ah ' + str(key))

    # to set the window title
    # self.canvas.set_window_title()

#    To open a file dialog box (within matplotlib) do (NOTE: NOT TESTED!):
#     import tkFileDialog as fd
#     fname = fd.askopenfilename(initialfile=files[0])
#     fname

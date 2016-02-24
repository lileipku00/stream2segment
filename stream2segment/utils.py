'''
Created on Feb 19, 2016

@author: riccardo
'''
# import yaml
import datetime as dt
# import dateutil.parser as dparser

# # good: dateutil.parser parses strings into datetime
# # bad: dateutil.parser checks the "Z" at the end as UTC timezone, and prints it in isoformat
# # bad: dateutil.parser returns everything as datetime, maybe sometimes we want a date
# def str2isodate(string):
#     dtm = dparser.parse(string, yearfirst=True, dayfirst=False, ignoretz=True)
#     dtm_str = dtm.isoformat()
#     if dtm_str[-1] == 'Z':
#         dtm_str = dtm_str[:-1]


def normalize_datestr(string, ignore_z=True, allow_space=True):
    # kind of redundant but allows unit testing
    try:
        if ignore_z and string[-1] == 'Z':
            string = string[:-1]

        if allow_space:
            string = string.replace(' ', 'T')
    except (TypeError, IndexError, AttributeError):
        pass

    return string


def to_datetime(string, ignore_z=True, allow_space=True):
    """
        Converts a date in string format (as returned by a fdnsws query) into
        a datetime python object. The inverse can be obtained by calling
        dt.isoformat() (which returns 'T' as date time separator, and optionally microseconds
        if they are not zero)
        Example:
        to_datetime("2016-06-01T09:04:00.5600Z")
        to_datetime("2016-06-01T09:04:00.5600")
        to_datetime("2016-06-01 09:04:00.5600Z")
        to_datetime("2016-06-01 09:04:00.5600Z")
        to_datetime("2016-06-01")
    """
    dtm = None
    string = normalize_datestr(string, ignore_z, allow_space)

    array = ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%Y-%m-%dT%H:%M:%S.%f']

    for dtformat in array:
        try:
            dtm = dt.datetime.strptime(string, dtformat)
            break
        except ValueError:  # as exce:
            pass
        except TypeError:  # as terr:
            return None

    return dtm

# # Original function
# def to_datetime(date_str):
#     """
#         Converts a date in string format (as returned by a fdnsws query) into
#         a datetime python object
#         Example:
#         to_datetime("2016-06-01T09:04:00.5600Z")
#         to_datetime("2016-06-01T09:04:00.5600")
#         to_datetime("2016-06-01 09:04:00.5600Z")
#         to_datetime("2016-06-01 09:04:00.5600Z")
#         to_datetime("2016-06-01")
#     """
#     # Note: dateutil.parser.parse(string, yearfirst=True, dayfirst=False, ignoretz=True)
#     # does ALMOST the same except that:
#     # ignoretz ignores all timezones, we want to ignore only Z
#     # '00-09-03 20:56:35.450686Z' is converted to datetime.datetime(2000, 9, 3, 20, 56, 35, 450686)
#     # whereas:
#     # datetime.datetime(0, 9, 3, 20, 56, 35, 450686) raises a ValueError which we want to have
#     # Thus, this function
#     try:
#         date_str = date_str.replace('-', ' ').replace('T', ' ')\
#             .replace(':', ' ').replace('.', ' ').replace('Z', '').split()
#         return dt.datetime(*(int(value) for value in date_str))
#     except (AttributeError, IndexError, ValueError, TypeError):
#         return None
# 
# # Original function stricter. Tries to be like a python parser BUT as fast as to_datetime above
# def to_datetime2(date_str, ignore_z=True, allow_space=True):
#     """
#         Converts a date in string format (as returned by a fdnsws query) into
#         a datetime python object
#         Example:
#         to_datetime("2016-06-01T09:04:00.5600Z")
#         to_datetime("2016-06-01T09:04:00.5600")
#         to_datetime("2016-06-01 09:04:00.5600Z")
#         to_datetime("2016-06-01 09:04:00.5600Z")
#         to_datetime("2016-06-01")
#     """
#     # Note: dateutil.parser.parse(string, yearfirst=True, dayfirst=False, ignoretz=True)
#     # does ALMOST the same except that:
#     # ignoretz ignores all timezones, we want to ignore only Z
#     # '00-09-03 20:56:35.450686Z' is converted to datetime.datetime(2000, 9, 3, 20, 56, 35, 450686)
#     # whereas:
#     # datetime.datetime(0, 9, 3, 20, 56, 35, 450686) raises a ValueError which we want to have
#     # Thus, this function
#     if ignore_z and date_str[-1] == 'Z':
#         date_str = date_str[:-1]
# 
#     if allow_space:
#         date_str = date_str.replace(' ', 'T')
#     dsplit = date_str.split('T')
# 
#     try:
#         assert len(dsplit) in (1, 2)
#         split1 = dsplit[0].split("-")
#         if len(dsplit) == 2:
#             split2 = dsplit[1].split(":")
#             split1.extend(split2[:-1])
#             split1.extend(split2[-1].split('.'))
#         return dt.datetime(*(int(value) for value in split1))
#     except (AssertionError, AttributeError, IndexError, ValueError, TypeError):
#         return None


# import time
# 
# if __name__ == "__main__":
#     import os
#     print os.path.abspath("seed")
#     
#     date_ = "2006-01-05" # "2006-01-05 12:34:56Z"
#     N = 10000
#     clock_ = time.clock()
#     for i in xrange(N):
#         str2isodate(date_)
# 
#     c1 = str(time.clock() - clock_)
#     print "str2isodate " + str(c1)
# 
#     clock_ = time.clock()
#     for i in xrange(N):
#         str2isodate_(date_)
# 
#     c1 = str(time.clock() - clock_)
#     print "str2isodate_ " + str(c1)
    
#     clock_ = time.clock()
#     for i in xrange(N):
#         to_datetime(date_)
# 
#     c1 = str(time.clock() - clock_)
#     print "to_datetime: " + str(c1)
    
#     clock_ = time.clock()
#     for i in xrange(N):
#         to_datetime2(date_)
# 
#     c1 = str(time.clock() - clock_)
#     print "to_datetime2: " + str(c1)
#  
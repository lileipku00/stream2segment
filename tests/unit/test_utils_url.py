#@PydevCodeAnalysisIgnore
'''
Created on Nov 18, 2016

@author: riccardo
'''
import unittest


import concurrent.futures
import time
import threading
import urllib2
import httplib
import mock
# from stream2segment import async
from stream2segment.utils.url import _ismainthread, read_async
from mock import patch
from itertools import product, cycle
import pytest

class Test(unittest.TestCase):


    def setUp(self):
        self.urls = ["http://sdgfjvkherkdfvsffd",
                     "http://www.google.com", 
#                      "http://www.apple.com",
#                      "http://www.microsoft.com",
#                      "http://www.amazon.com",
#                      "http://www.facebook.com"
                    ]
        self.thread = threading.current_thread()
        
        self.successes = []
        self.errors = []
        self.cancelled = []
        self.ondone_return_value = None
        
        self.ondone = mock.Mock()
        self.ondone.side_effect = self._ondone
        
        self.oncanc = mock.Mock()
        self.oncanc.side_effect = self._oncanc
        
        self.patcher = patch('stream2segment.utils.url.urllib2.urlopen')
        self.mock_urlopen = self.patcher.start()
        #add cleanup (in case tearDown is not 
        self.addCleanup(Test.cleanup, self.patcher)
        
    @staticmethod
    def cleanup(*patchers):
        for patcher in patchers:
            patcher.stop()
        
    def _ondone(self, obj, exc, res, url):
        assert _ismainthread()
        if exc:
            if not res:
                raise exc
            self.errors.append(exc)
        else:
            self.successes.append(res)
        return self.ondone_return_value
    
    def _oncanc(self, obj,url):
        assert _ismainthread()
        self.cancelled.append(obj)

    def tearDown(self):
        self.successes = []
        self.errors = []
        self.cancelled = []
        pass

    def config_urlopen(self, read_side_effect_as_list):
        a = mock.Mock()
        a.read.side_effect = cycle(read_side_effect_as_list)  # returns each item in list
        self.mock_urlopen.return_value = a

    @property
    def mock_urlread(self):
        return self.mock_urlopen.return_value.read

    def test_mocking_urlread(self):
        """Tests onsuccess. WE mock urllib2urlopen.read to return user defined strings"""
        
        data = ['none', '', 'google', '']  # supply an empty string otherwise urllib.read does not stop
        self.config_urlopen(data)

        # self.urls has a valid url (which should execute onsuccess) and an invalid one
        # which should execute onerror)
        read_async(self.urls, self.ondone)

        assert len(self.successes) == 2
        assert sorted(data) == ['', ''] + sorted(self.successes)  # sort them as the order might differ
        assert self.mock_urlread.call_count == len(data)

    def test_urlerrors(self):
        """Tests onerror. WE mock urllib2urlopen.read to raise an excpected Exception"""
        
        self.config_urlopen([urllib2.URLError("")])
        
        # self.urls has a valid url (which should execute onsuccess) and an invalid one
        # which should execute onerror)
        read_async(self.urls, self.ondone)

        assert len(self.errors) == 2
        assert self.mock_urlread.call_count == len(self.urls)


    def test_onerror_onsuccess_returning_false(self):
        """Tests onerror onsuccess returning False, i.e. onsuccess must be called once"""
        
        data = ['none', '', 'google', '']  # supply an empty string otherwise urllib.read does not stop
        self.config_urlopen(data)

        self.ondone_return_value = lambda obj: True
        # self.urls has a valid url (which should execute onsuccess) and an invalid one
        # which should execute onerror)
        read_async(self.urls, self.ondone, oncanc=self.oncanc)

        assert len(self.successes) == 1  # or alternatively:
        assert self.ondone.call_count == 1
        assert len(self.cancelled)
        
    def test_general_exception(self):
        self.config_urlopen([ValueError("")])
        
        # self.urls has a valid url (which should execute onsuccess) and an invalid one
        # which should execute onerror)
        with pytest.raises(ValueError):
            read_async(self.urls, self.ondone)
        
#     @patch('stream2segment.utils.url.urllib2.urlopen')
#     def tst_onerror_onsuccess_returning_false2(self, mock_urlopen):
#         """Tests onerror onsuccess returning False, i.e. the they must be called N times, where
#         N < len(self.urls). WE mock urllib2urlopen.read with a time to wait in order to be sure
#         that urllib2urlopen.read is also called LESS times than len(self.urls)"""
# 
#         # I want to test the above, PLUS that urllib2.urlopen.read is not called the total
#         # amount of time. For that, let's the working thread make some work, otherwise we do not
#         # see the difference as all wrkers have finished before calling the first 'onsuccess'
# 
#         
#         # increase the number of url. Doesnt matter their names, out an int:
#         urls = [str(i) for i in xrange(10)]
# 
#         # we cannot set assert urlib.read.call_count < expected_urllib_read_call_count
#         # BECAUSE it might be equal (if all read are executed before first call to onsuccess)
#         # On the other hand, testing that urlib.read.call_count <= expected_urllib_read_call_count
#         # returns True also if they are equal, which does not assures me the onsuccess function
#         # return value + threading works as expected. So we do some heuristic: we set a combination
#         # of urls each and urls lengths. We assert that for  *at least one* holds:
#         # urlib.read.call_count < expected_urllib_read_call_count
#         how_many_one_strictly_lower_than = 0
#         for blocksize, reads_per_url in product([1, 1024], [1, 10, 100]):
#             # needs this to rest:
#             self.setUp()
#             
#             # set the expected times we weill call urllib2.read:
#             expected_urllib_read_call_count = len(urls) * reads_per_url
#             # build return values which satisfy: for each url, read must be called reads_per_url
#             # times
#             data = len(urls) * (['x' * blocksize] * (reads_per_url-1) + [''])  # supply an empty string otherwise urllib.read does not stop
#             urllib2_urlopen_read = mock.Mock()
#             urllib2_urlopen_read.read.side_effect = data  # _read
#             mock_urlopen.return_value = urllib2_urlopen_read
#     
#             self.onsuccess_return_value = self.onerror_return_value = False
#             # self.urls has a valid url (which should execute onsuccess) and an invalid one
#             # which should execute onerror)
#             read_async(urls, self.onsuccess, self.onerror, blocksize=blocksize)
#     
#             assert self.onsuccess.call_count == 1
#             assert urllib2_urlopen_read.read.call_count <= expected_urllib_read_call_count
#             if urllib2_urlopen_read.read.call_count < expected_urllib_read_call_count:
#                 how_many_one_strictly_lower_than +=1
# 
#         assert how_many_one_strictly_lower_than > 0

if __name__ == "__main__":
    #import sys;sys.argv = ['', 'Test.testName']
    unittest.main()
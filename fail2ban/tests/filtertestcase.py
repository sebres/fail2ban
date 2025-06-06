# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: t -*-
# vi: set ft=python sts=4 ts=4 sw=4 noet :

# This file is part of Fail2Ban.
#
# Fail2Ban is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Fail2Ban is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Fail2Ban; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

# Fail2Ban developers

__copyright__ = "Copyright (c) 2004 Cyril Jaquier; 2012 Yaroslav Halchenko"
__license__ = "GPL"

from builtins import open as fopen
import unittest
import os
import re
import sys
import time, datetime
import tempfile
import uuid

try:
	from ..server.filtersystemd import journal, _globJournalFiles
except ImportError:
	journal = None

from ..helpers import uni_bytes
from ..server.jail import Jail
from ..server.filterpoll import FilterPoll
from ..server.filter import FailTicket, Filter, FileFilter, FileContainer
from ..server.failmanager import FailManagerEmpty
from ..server.ipdns import asip, getfqdn, DNSUtils, IPAddr, IPAddrSet
from ..server.mytime import MyTime
from ..server.utils import Utils, uni_decode
from .databasetestcase import getFail2BanDb
from .utils import setUpMyTime, tearDownMyTime, mtimesleep, with_alt_time, with_tmpdir, LogCaptureTestCase, \
	logSys as DefLogSys, CONFIG_DIR as STOCK_CONF_DIR
from .dummyjail import DummyJail

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), "files")


# yoh: per Steven Hiscocks's insight while troubleshooting
# https://github.com/fail2ban/fail2ban/issues/103#issuecomment-15542836
# adding a sufficiently large buffer might help to guarantee that
# writes happen atomically.
def open(*args):
	"""Overload built in open so we could assure sufficiently large buffer

	Explicit .flush would be needed to assure that changes leave the buffer
	"""
	if len(args) == 2:
		# ~50kB buffer should be sufficient for all tests here.
		args = args + (50000,)
	return fopen(*args)


def _killfile(f, name):
	try:
		f.close()
	except:
		pass
	try:
		os.unlink(name)
	except:
		pass

	# there might as well be the .bak file
	if os.path.exists(name + '.bak'):
		_killfile(None, name + '.bak')


_maxWaitTime = unittest.F2B.maxWaitTime


class _tmSerial():
	_last_s = -0x7fffffff
	_last_m = -0x7fffffff
	_str_s = ""
	_str_m = ""
	@staticmethod
	def _tm(time):
		# ## strftime it too slow for large time serializer :
		# return MyTime.time2str(time)
		c = _tmSerial
		sec = (time % 60)
		if c._last_s == time - sec:
			return "%s%02u" % (c._str_s, sec)
		mt = (time % 3600)
		if c._last_m == time - mt:
			c._last_s = time - sec
			c._str_s = "%s%02u:" % (c._str_m, mt // 60)
			return "%s%02u" % (c._str_s, sec)
		c._last_m = time - mt
		c._str_m = datetime.datetime.fromtimestamp(time).strftime("%Y-%m-%d %H:")
		c._last_s = time - sec
		c._str_s = "%s%02u:" % (c._str_m, mt // 60)
		return "%s%02u" % (c._str_s, sec)

_tm = _tmSerial._tm
_tmb = lambda t: uni_bytes(_tm(t))


def _assert_equal_entries(utest, found, output, count=None):
	"""Little helper to unify comparisons with the target entries

	and report helpful failure reports instead of millions of seconds ;)
	"""
	utest.assertEqual(found[0], output[0])            # IP
	utest.assertEqual(found[1], count or output[1])   # count
	found_time, output_time = \
				MyTime.localtime(found[2]),\
				MyTime.localtime(output[2])
	try:
		utest.assertEqual(found_time, output_time)
	except AssertionError as e:
		# assert more structured:
		utest.assertEqual((float(found[2]), found_time), (float(output[2]), output_time))
	if len(output) > 3 and count is None: # match matches
		# do not check if custom count (e.g. going through them twice)
		if os.linesep != '\n' or sys.platform.startswith('cygwin'):
			# on those where text file lines end with '\r\n', remove '\r'
			srepr = lambda x: repr(x).replace(r'\r', '')
		else:
			srepr = repr
		utest.assertEqual(srepr(found[3]), srepr(output[3]))


def _ticket_tuple(ticket):
	"""Create a tuple for easy comparison from fail ticket
	"""
	attempts = ticket.getAttempt()
	date = ticket.getTime()
	ip = ticket.getID()
	matches = ticket.getMatches()
	return (ip, attempts, date, matches)


def _assert_correct_last_attempt(utest, filter_, output, count=None):
	"""Additional helper to wrap most common test case

	Test filter to contain target ticket
	"""
	# one or multiple tickets:
	if not isinstance(output[0], (tuple,list)):
		tickcount = 1
		failcount = (count if count else output[1])
	else:
		tickcount = len(output)
		failcount = (count if count else sum((o[1] for o in output)))

	found = []
	if isinstance(filter_, DummyJail):
		# get fail ticket from jail
		found.append(_ticket_tuple(filter_.getFailTicket()))
	else:
		# when we are testing without jails wait for failures (up to max time)
		if filter_.jail:
			while True:
				t = filter_.jail.getFailTicket()
				if not t: break
				found.append(_ticket_tuple(t))
		if found:
			tickcount -= len(found)
		if tickcount > 0:
			Utils.wait_for(
				lambda: filter_.failManager.getFailCount() >= (tickcount, failcount),
				_maxWaitTime(10))
			# get fail ticket(s) from filter
			while tickcount:
				try:
					found.append(_ticket_tuple(filter_.failManager.toBan()))
				except FailManagerEmpty:
					break
				tickcount -= 1

	if not isinstance(output[0], (tuple,list)):
		utest.assertEqual(len(found), 1)
		_assert_equal_entries(utest, found[0], output, count)
	else:
		utest.assertEqual(len(found), len(output))
		# sort by string representation of ip (multiple failures with different ips):
		found = sorted(found, key=lambda x: str(x))
		output = sorted(output, key=lambda x: str(x))
		for f, o in zip(found, output):
			_assert_equal_entries(utest, f, o)


def _copy_lines_between_files(in_, fout, n=None, skip=0, mode='a', terminal_line="", lines=None):
	"""Copy lines from one file to another (which might be already open)

	Returns open fout
	"""
	# on old Python st_mtime is int, so we should give at least 1 sec so
	# polling filter could detect the change
	mtimesleep()
	if terminal_line is not None:
		terminal_line = uni_bytes(terminal_line)
	if isinstance(in_, str): # pragma: no branch - only used with str in test cases
		fin = open(in_, 'rb')
	else:
		fin = in_
	# Skip
	for i in range(skip):
		fin.readline()
	# Read
	i = 0
	if lines:
		lines = list(map(uni_bytes, lines))
	else:
		lines = []
	while n is None or i < n:
		l = fin.readline().rstrip(b'\r\n')
		if terminal_line is not None and l == terminal_line:
			break
		lines.append(l)
		i += 1
	# Write: all at once and flush
	if isinstance(fout, str):
		fout = open(fout, mode+'b')
	DefLogSys.debug('  ++ write %d test lines', len(lines))
	fout.write(b'\n'.join(lines)+b'\n')
	fout.flush()
	if isinstance(in_, str): # pragma: no branch - only used with str in test cases
		# Opened earlier, therefore must close it
		fin.close()
	# to give other threads possibly some time to crunch
	time.sleep(Utils.DEFAULT_SHORT_INTERVAL)
	return fout


TEST_JOURNAL_FIELDS = {
  "SYSLOG_IDENTIFIER": "fail2ban-testcases",
	"PRIORITY": "7",
}
def _copy_lines_to_journal(in_, fields={},n=None, skip=0, terminal_line=""): # pragma: systemd no cover
	"""Copy lines from one file to systemd journal

	Returns None
	"""
	if isinstance(in_, str): # pragma: no branch - only used with str in test cases
		fin = open(in_, 'rb')
	else:
		fin = in_
	# Required for filtering
	fields.update(TEST_JOURNAL_FIELDS)
	# Skip
	for i in range(skip):
		fin.readline()
	# Read/Write
	i = 0
	while n is None or i < n:
		l = fin.readline().decode('UTF-8', 'replace').rstrip('\r\n')
		if terminal_line is not None and l == terminal_line:
			break
		journal.send(MESSAGE=l.strip(), **fields)
		i += 1
	if isinstance(in_, str): # pragma: no branch - only used with str in test cases
		# Opened earlier, therefore must close it
		fin.close()


#
#  Actual tests
#

class BasicFilter(unittest.TestCase):

	def setUp(self):
		super(BasicFilter, self).setUp()
		self.filter = Filter(None)

	def testGetSetUseDNS(self):
		# default is warn
		self.assertEqual(self.filter.getUseDns(), 'warn')
		self.filter.setUseDns(True)
		self.assertEqual(self.filter.getUseDns(), 'yes')
		self.filter.setUseDns(False)
		self.assertEqual(self.filter.getUseDns(), 'no')

	def testGetSetDatePattern(self):
		self.assertEqual(self.filter.getDatePattern(),
			(None, "Default Detectors"))
		self.filter.setDatePattern(r"^%Y-%m-%d-%H%M%S\.%f %z **")
		self.assertEqual(self.filter.getDatePattern(),
			(r"^%Y-%m-%d-%H%M%S\.%f %z **",
			r"^Year-Month-Day-24hourMinuteSecond\.Microseconds Zone offset **"))

	def testGetSetLogTimeZone(self):
		self.assertEqual(self.filter.getLogTimeZone(), None)
		self.filter.setLogTimeZone('UTC')
		self.assertEqual(self.filter.getLogTimeZone(), 'UTC')
		self.filter.setLogTimeZone('UTC-0400')
		self.assertEqual(self.filter.getLogTimeZone(), 'UTC-0400')
		self.filter.setLogTimeZone('UTC+0200')
		self.assertEqual(self.filter.getLogTimeZone(), 'UTC+0200')
		self.assertRaises(ValueError, self.filter.setLogTimeZone, 'not-a-time-zone')

	def testAssertWrongTime(self):
		self.assertRaises(AssertionError, 
			lambda: _assert_equal_entries(self, 
				('1.1.1.1', 1, 1421262060.0), 
				('1.1.1.1', 1, 1421262059.0), 
			1)
		)

	def testTest_tm(self):
		unittest.F2B.SkipIfFast()
		## test function "_tm" works correct (returns the same as slow strftime):
		for i in range(1417512352, (1417512352 // 3600 + 3) * 3600):
			tm = MyTime.time2str(i)
			if _tm(i) != tm: # pragma: no cover - never reachable
				self.assertEqual((_tm(i), i), (tm, i))

	def testWrongCharInTupleLine(self):
		## line tuple has different types (ascii after ascii / unicode):
		for a1 in ('', '', b''):
			for a2 in ('2016-09-05T20:18:56', '2016-09-05T20:18:56', b'2016-09-05T20:18:56'):
				for a3 in (
					'Fail for "g\xc3\xb6ran" from 192.0.2.1', 
					'Fail for "g\xc3\xb6ran" from 192.0.2.1',
					b'Fail for "g\xc3\xb6ran" from 192.0.2.1'
				):
					# join should work if all arguments have the same type:
					"".join([uni_decode(v) for v in (a1, a2, a3)])


class IgnoreIP(LogCaptureTestCase):

	def setUp(self):
		"""Call before every test case."""
		LogCaptureTestCase.setUp(self)
		self.jail = DummyJail()
		self.filter = FileFilter(self.jail)
		self.filter.ignoreSelf = False

	def testIgnoreSelfIP(self):
		ipList = ("127.0.0.1",)
		# test ignoreSelf is false:
		for ip in ipList:
			self.assertFalse(self.filter.inIgnoreIPList(ip))
			self.assertNotLogged("[%s] Ignore %s by %s" % (self.jail.name, ip, "ignoreself rule"))
		# test ignoreSelf with true:
		self.filter.ignoreSelf = True
		self.pruneLog()
		for ip in ipList:
			self.assertTrue(self.filter.inIgnoreIPList(ip))
			self.assertLogged("[%s] Ignore %s by %s" % (self.jail.name, ip, "ignoreself rule"))

	def testIgnoreIPOK(self):
		ipList = "127.0.0.1", "192.168.0.1", "255.255.255.255", "99.99.99.99"
		for ip in ipList:
			self.filter.addIgnoreIP(ip)
			self.assertTrue(self.filter.inIgnoreIPList(ip))
			self.assertLogged("[%s] Ignore %s by %s" % (self.jail.name, ip, "ip"))

	def testIgnoreIPNOK(self):
		ipList = "", "999.999.999.999", "abcdef.abcdef", "192.168.0."
		for ip in ipList:
			self.filter.addIgnoreIP(ip)
			self.assertFalse(self.filter.inIgnoreIPList(ip))
		if not unittest.F2B.no_network: # pragma: no cover
			self.assertLogged(
				'Unable to find a corresponding IP address for 999.999.999.999',
				'Unable to find a corresponding IP address for abcdef.abcdef',
				'Unable to find a corresponding IP address for 192.168.0.', all=True)

	def testIgnoreIPCIDR(self):
		self.filter.addIgnoreIP('192.168.1.0/25')
		self.assertTrue(self.filter.inIgnoreIPList('192.168.1.0'))
		self.assertTrue(self.filter.inIgnoreIPList('192.168.1.1'))
		self.assertTrue(self.filter.inIgnoreIPList('192.168.1.127'))
		self.assertFalse(self.filter.inIgnoreIPList('192.168.1.128'))
		self.assertFalse(self.filter.inIgnoreIPList('192.168.1.255'))
		self.assertFalse(self.filter.inIgnoreIPList('192.168.0.255'))

	def testIgnoreIPMask(self):
		self.filter.addIgnoreIP('192.168.1.0/255.255.255.128')
		self.assertTrue(self.filter.inIgnoreIPList('192.168.1.0'))
		self.assertTrue(self.filter.inIgnoreIPList('192.168.1.1'))
		self.assertTrue(self.filter.inIgnoreIPList('192.168.1.127'))
		self.assertFalse(self.filter.inIgnoreIPList('192.168.1.128'))
		self.assertFalse(self.filter.inIgnoreIPList('192.168.1.255'))
		self.assertFalse(self.filter.inIgnoreIPList('192.168.0.255'))

	def testWrongIPMask(self):
		self.filter.addIgnoreIP('192.168.1.0/255.255.0.0')
		self.assertRaises(ValueError, self.filter.addIgnoreIP, '192.168.1.0/255.255.0.128')

	def testIgnoreIPDNS(self):
		# test subnets are pre-cached (as IPAddrSet), so it shall work even without network:
		for dns in ("test-subnet-a", "test-subnet-b"):
			self.filter.addIgnoreIP(dns)
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('192.0.2.1')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('192.0.2.7')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('192.0.2.16')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('192.0.2.23')))
		self.assertFalse(self.filter.inIgnoreIPList(IPAddr('192.0.2.8')))
		self.assertFalse(self.filter.inIgnoreIPList(IPAddr('192.0.2.15')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:db8::00')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:db8::07')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:0db8:0000:0000:0000:0000:0000:0000')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:0db8:0000:0000:0000:0000:0000:0007')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:db8::10')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:db8::17')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:0db8:0000:0000:0000:0000:0000:0010')))
		self.assertTrue(self.filter.inIgnoreIPList(IPAddr('2001:0db8:0000:0000:0000:0000:0000:0017')))
		self.assertFalse(self.filter.inIgnoreIPList(IPAddr('2001:db8::08')))
		self.assertFalse(self.filter.inIgnoreIPList(IPAddr('2001:db8::0f')))
		self.assertFalse(self.filter.inIgnoreIPList(IPAddr('2001:0db8:0000:0000:0000:0000:0000:0008')))
		self.assertFalse(self.filter.inIgnoreIPList(IPAddr('2001:0db8:0000:0000:0000:0000:0000:000f')))

	# to test several IPs in ip-set from file "files/test-ign-ips-file":
	TEST_IPS_IGN_FILE = {
		'127.0.0.1': True,
		'127.255.255.255': True,
		'127.0.0.1/8': True,
		'192.0.2.1': True,
		'192.0.2.7': True,
		'192.0.2.0/29': True,
		'192.0.2.16': True,
		'192.0.2.23': True,
		'192.0.2.200': True,
		'192.0.2.216': True,
		'192.0.2.223': True,
		'192.0.2.216/29': True,
		'192.0.2.8': False,
		'192.0.2.15': False,
		'192.0.2.100': False,
		'192.0.2.224': False,
		'::1': True,
		'2001:db8::00': True,
		'2001:db8::07': True,
		'2001:db8::0/125': True,
		'2001:0db8:0000:0000:0000:0000:0000:0000': True,
		'2001:0db8:0000:0000:0000:0000:0000:0007': True,
		'2001:db8::10': True,
		'2001:db8::17': True,
		'2001:0db8:0000:0000:0000:0000:0000:0010': True,
		'2001:0db8:0000:0000:0000:0000:0000:0017': True,
		'2001:db8::c8': True,
		'2001:db8::d8': True,
		'2001:db8::df': True,
		'2001:db8::d8/125': True,
		'2001:0db8:0000:0000:0000:0000:0000:00d8': True,
		'2001:0db8:0000:0000:0000:0000:0000:00df': True,
		'2001:db8::08': False,
		'2001:db8::0f': False,
		'2001:0db8:0000:0000:0000:0000:0000:0008': False,
		'2001:0db8:0000:0000:0000:0000:0000:000f': False,
		'2001:db8::e0': False,
		'2001:0db8:0000:0000:0000:0000:0000:00e0': False,
	}

	def testIgnoreIPFileIPAddr(self):
		fname = 'file://' + os.path.join(TEST_FILES_DIR, "test-ign-ips-file")
		self.filter.ignoreSelf = False
		self.filter.addIgnoreIP(fname)
		for ip, v in IgnoreIP.TEST_IPS_IGN_FILE.items():
			self.assertEqual(self.filter.inIgnoreIPList(IPAddr(ip)), v, ("for %r in ignoreip, file://test-ign-ips-file)" % (ip,)))
		# now remove it:
		self.filter.delIgnoreIP(fname)
		for ip in IgnoreIP.TEST_IPS_IGN_FILE.keys():
			self.assertEqual(self.filter.inIgnoreIPList(IPAddr(ip)), False, ("for %r ignoreip, without file://test-ign-ips-file)" % (ip,)))

	def testIgnoreInProcessLine(self):
		setUpMyTime()
		try:
			self.filter.addIgnoreIP('192.168.1.0/25')
			self.filter.addFailRegex('<HOST>')
			self.filter.setDatePattern(r'{^LN-BEG}EPOCH')
			self.filter.processLineAndAdd('1387203300.222 192.168.1.32')
			self.assertLogged('Ignore 192.168.1.32')
		finally:
			tearDownMyTime()

	def _testTimeJump(self, inOperation=False):
		try:
			self.filter.addFailRegex('^<HOST>')
			self.filter.setDatePattern(r'{^LN-BEG}%Y-%m-%d %H:%M:%S(?:\s*%Z)?\s')
			self.filter.setFindTime(10); # max 10 seconds back
			self.filter.setMaxRetry(5); # don't ban here
			self.filter.inOperation = inOperation
			#
			self.pruneLog('[phase 1] DST time jump')
			# check local time jump (DST hole):
			MyTime.setTime(1572137999)
			self.filter.processLineAndAdd('2019-10-27 02:59:59 192.0.2.5'); # +1 = 1
			MyTime.setTime(1572138000)
			self.filter.processLineAndAdd('2019-10-27 02:00:00 192.0.2.5'); # +1 = 2
			MyTime.setTime(1572138001)
			self.filter.processLineAndAdd('2019-10-27 02:00:01 192.0.2.5'); # +1 = 3
			self.assertLogged(
				'Current failures from 1 IPs (IP:count): 192.0.2.5:1', 
				'Current failures from 1 IPs (IP:count): 192.0.2.5:2', 
				'Current failures from 1 IPs (IP:count): 192.0.2.5:3',
				"Total # of detected failures: 3.", all=True, wait=True)
			self.assertNotLogged('Ignore line')
			#
			self.pruneLog('[phase 2] UTC time jump (NTP correction)')
			# check time drifting backwards (NTP correction):
			MyTime.setTime(1572210000)
			self.filter.processLineAndAdd('2019-10-27 22:00:00 CET 192.0.2.6'); # +1 = 1
			MyTime.setTime(1572200000)
			self.filter.processLineAndAdd('2019-10-27 22:00:01 CET 192.0.2.6'); # +1 = 2 (logged before correction)
			self.filter.processLineAndAdd('2019-10-27 19:13:20 CET 192.0.2.6'); # +1 = 3 (logged after correction)
			self.filter.processLineAndAdd('2019-10-27 19:13:21 CET 192.0.2.6'); # +1 = 4
			self.assertLogged(
				'192.0.2.6:1', '192.0.2.6:2', '192.0.2.6:3', '192.0.2.6:4', 
				"Total # of detected failures: 7.", all=True, wait=True)
			self.assertNotLogged('Ignore line')
		finally:
			tearDownMyTime()
	def testTimeJump(self):
		self._testTimeJump(inOperation=False)
	def testTimeJump_InOperation(self):
		self._testTimeJump(inOperation=True)

	def testWrongTimeOrTZ(self):
		try:
			self.filter.addFailRegex('fail from <ADDR>$')
			self.filter.setDatePattern(r'{^LN-BEG}%Y-%m-%d %H:%M:%S(?:\s*%Z)?\s')
			self.filter.setMaxRetry(50); # don't ban here
			self.filter.inOperation = True; # real processing (all messages are new)
			# current time is 1h later than log-entries:
			MyTime.setTime(1572138000+3600)
			#
			self.pruneLog("[phase 1] simulate wrong TZ")
			for i in (1,2,3):
				self.filter.processLineAndAdd('2019-10-27 02:00:00 fail from 192.0.2.15'); # +3 = 3
			self.assertLogged(
				"Detected a log entry 1h before the current time in operation mode. This looks like a timezone problem.",
				"Please check a jail for a timing issue.",
				"192.0.2.15:1", "192.0.2.15:2", "192.0.2.15:3",
				"Total # of detected failures: 3.", all=True, wait=True)
			#
			setattr(self.filter, "_next_simByTimeWarn", -1)
			self.pruneLog("[phase 2] wrong TZ given in log")
			for i in (1,2,3):
				self.filter.processLineAndAdd('2019-10-27 04:00:00 GMT fail from 192.0.2.16'); # +3 = 6
			self.assertLogged(
				"Detected a log entry 2h after the current time in operation mode. This looks like a timezone problem.",
				"Please check a jail for a timing issue.",
				"192.0.2.16:1", "192.0.2.16:2", "192.0.2.16:3",
				"Total # of detected failures: 6.", all=True, wait=True)
			self.assertNotLogged("Found a match but no valid date/time found")
			#
			self.pruneLog("[phase 3] other timestamp (don't match datepattern), regex matches")
			for i in range(3):
				self.filter.processLineAndAdd('27.10.2019 04:00:00 fail from 192.0.2.17'); # +3 = 9
			self.assertLogged(
				"Found a match but no valid date/time found",
				"Match without a timestamp:",
				"192.0.2.17:1", "192.0.2.17:2", "192.0.2.17:3",
				"Total # of detected failures: 9.", all=True, wait=True)
			#
			phase = 3
			for delta, expect in (
				(-90*60, "timezone"), #90 minutes after
				(-60*60, "timezone"), #60 minutes after
				(-10*60, "timezone"), #10 minutes after
				(-59,    None),       #59 seconds after
				(59,     None),       #59 seconds before
				(61,     "latency"),  #>1 minute before
				(55*60,  "latency"),  #55 minutes before
				(90*60,  "timezone")  #90 minutes before
			):
				phase += 1
				MyTime.setTime(1572138000+delta)
				setattr(self.filter, "_next_simByTimeWarn", -1)
				self.pruneLog('[phase {phase}] log entries offset by {delta}s'.format(phase=phase, delta=delta))
				self.filter.processLineAndAdd('2019-10-27 02:00:00 fail from 192.0.2.15');
				self.assertLogged("Found 192.0.2.15", wait=True)
				if expect:
					self.assertLogged(("timezone problem", "latency problem")[int(expect == "latency")], all=True)
					self.assertNotLogged(("timezone problem", "latency problem")[int(expect != "latency")], all=True)
				else:
					self.assertNotLogged("timezone problem", "latency problem", all=True)
		finally:
			tearDownMyTime()

	def testAddAttempt(self):
		self.filter.setMaxRetry(3)
		for i in range(1, 1+3):
			self.filter.addAttempt('192.0.2.1')
			self.assertLogged('Attempt 192.0.2.1', '192.0.2.1:%d' % i, all=True, wait=True)
		self.jail.actions._Actions__checkBan()
		self.assertLogged('Ban 192.0.2.1', wait=True)

	def testIgnoreCommand(self):
		self.filter.ignoreCommand = sys.executable + ' ' + os.path.join(TEST_FILES_DIR, "ignorecommand.py <ip>")
		self.assertTrue(self.filter.inIgnoreIPList("10.0.0.1"))
		self.assertFalse(self.filter.inIgnoreIPList("10.0.0.0"))
		self.assertLogged("returned successfully 0", "returned successfully 1", all=True)
		self.pruneLog()
		self.assertFalse(self.filter.inIgnoreIPList(""))
		self.assertLogged("usage: ignorecommand IP", "returned 10", all=True)
	
	def testIgnoreCommandForTicket(self):
		# by host of IP (2001:db8::1 and 2001:db8::ffff map to "test-host" and "test-other" in the test-suite):
		self.filter.ignoreCommand = 'if [ "<ip-host>" = "test-host" ]; then exit 0; fi; exit 1'
		self.pruneLog()
		self.assertTrue(self.filter.inIgnoreIPList(FailTicket("2001:db8::1")))
		self.assertLogged("returned successfully 0")
		self.pruneLog()
		self.assertFalse(self.filter.inIgnoreIPList(FailTicket("2001:db8::ffff")))
		self.assertLogged("returned successfully 1")
		# by user-name (ignore tester):
		self.filter.ignoreCommand = 'if [ "<F-USER>" = "tester" ]; then exit 0; fi; exit 1'
		self.pruneLog()
		self.assertTrue(self.filter.inIgnoreIPList(FailTicket("tester", data={'user': 'tester'})))
		self.assertLogged("returned successfully 0")
		self.pruneLog()
		self.assertFalse(self.filter.inIgnoreIPList(FailTicket("root", data={'user': 'root'})))
		self.assertLogged("returned successfully 1", all=True)

	def testIgnoreCache(self):
		# like both test-cases above, just cached (so once per key)...
		self.filter.ignoreCache = {"key":"<ip>"}
		self.filter.ignoreCommand = 'if [ "<ip>" = "10.0.0.1" ]; then exit 0; fi; exit 1'
		for i in range(5):
			self.pruneLog()
			self.assertTrue(self.filter.inIgnoreIPList("10.0.0.1"))
			self.assertFalse(self.filter.inIgnoreIPList("10.0.0.0"))
			if not i:
				self.assertLogged("returned successfully 0", "returned successfully 1", all=True)
			else:
				self.assertNotLogged("returned successfully 0", "returned successfully 1", all=True)
		# by host of IP:
		self.filter.ignoreCache = {"key":"<ip-host>"}
		self.filter.ignoreCommand = 'if [ "<ip-host>" = "test-host" ]; then exit 0; fi; exit 1'
		for i in range(5):
			self.pruneLog()
			self.assertTrue(self.filter.inIgnoreIPList(FailTicket("2001:db8::1")))
			self.assertFalse(self.filter.inIgnoreIPList(FailTicket("2001:db8::ffff")))
			if not i:
				self.assertLogged("returned successfully")
			else:
				self.assertNotLogged("returned successfully")
		# by user-name:
		self.filter.ignoreCache = {"key":"<F-USER>", "max-count":"10", "max-time":"1h"}
		self.assertEqual(self.filter.ignoreCache, ["<F-USER>", 10, 60*60])
		self.filter.ignoreCommand = 'if [ "<F-USER>" = "tester" ]; then exit 0; fi; exit 1'
		for i in range(5):
			self.pruneLog()
			self.assertTrue(self.filter.inIgnoreIPList(FailTicket("tester", data={'user': 'tester'})))
			self.assertFalse(self.filter.inIgnoreIPList(FailTicket("root", data={'user': 'root'})))
			if not i:
				self.assertLogged("returned successfully")
			else:
				self.assertNotLogged("returned successfully")

	def testIgnoreCauseOK(self):
		ip = "51.159.55.100"
		for ignore_source in ["dns", "ip", "command"]:
			self.filter.logIgnoreIp(ip, True, ignore_source=ignore_source)
			self.assertLogged("[%s] Ignore %s by %s" % (self.jail.name, ip, ignore_source))

	def testIgnoreCauseNOK(self):
		self.filter.logIgnoreIp("fail2ban.org", False, ignore_source="NOT_LOGGED")
		self.assertNotLogged("[%s] Ignore %s by %s" % (self.jail.name, "fail2ban.org", "NOT_LOGGED"))


class IgnoreIPDNS(LogCaptureTestCase):

	def setUp(self):
		"""Call before every test case."""
		unittest.F2B.SkipIfNoNetwork()
		LogCaptureTestCase.setUp(self)
		self.jail = DummyJail()
		self.filter = FileFilter(self.jail)

	def testIgnoreIPDNS(self):
		for dns in ("www.epfl.ch", "fail2ban.org"):
			self.filter.addIgnoreIP(dns)
			ips = DNSUtils.dnsToIp(dns)
			self.assertTrue(len(ips) > 0)
			# for each ip from dns check ip ignored:
			for ip in ips:
				ip = str(ip)
				DefLogSys.debug('  ++ positive case for %s', ip)
				self.assertTrue(self.filter.inIgnoreIPList(ip))
				# check another ips (with increment/decrement of first/last part) not ignored:
				iparr = []
				ip2 = re.search(r'^([^.:]+)([.:])(.*?)([.:])([^.:]+)$', ip)
				if ip2:
					ip2 = ip2.groups()
					for o in (0, 4):
						for i in (1, -1):
							ipo = list(ip2)
							if ipo[1] == '.':
								ipo[o] = str(int(ipo[o])+i)
							else:
								ipo[o] = '%x' % (int(ipo[o], 16)+i)
							ipo = ''.join(ipo)
							if ipo not in ips:
								iparr.append(ipo)
				self.assertTrue(len(iparr) > 0)
				for ip in iparr:
					DefLogSys.debug('  -- negative case for %s', ip)
					self.assertFalse(self.filter.inIgnoreIPList(str(ip)))

	def testIgnoreCmdApacheFakegooglebot(self):
		unittest.F2B.SkipIfCfgMissing(stock=True)
		cmd = os.path.join(STOCK_CONF_DIR, "filter.d/ignorecommands/apache-fakegooglebot")
		## below test direct as python module:
		mod = Utils.load_python_module(cmd)
		self.assertFalse(mod.is_googlebot(*mod.process_args([cmd, "128.178.222.69"])))
		self.assertFalse(mod.is_googlebot(*mod.process_args([cmd, "192.0.2.1"])))
		self.assertFalse(mod.is_googlebot(*mod.process_args([cmd, "192.0.2.1", 0.1])))
		bot_ips = ['66.249.66.1']
		for ip in bot_ips:
			self.assertTrue(mod.is_googlebot(*mod.process_args([cmd, str(ip)])), "test of googlebot ip %s failed" % ip)
		self.assertRaises(ValueError, lambda: mod.is_googlebot(*mod.process_args([cmd])))
		self.assertRaises(ValueError, lambda: mod.is_googlebot(*mod.process_args([cmd, "192.0"])))
		## via command:
		self.filter.ignoreCommand = cmd + " <ip>"
		for ip in bot_ips:
			self.assertTrue(self.filter.inIgnoreIPList(str(ip)), "test of googlebot ip %s failed" % ip)
			self.assertLogged('-- returned successfully')
			self.pruneLog()
		self.assertFalse(self.filter.inIgnoreIPList("192.0"))
		self.assertLogged('Argument must be a single valid IP.')
		self.pruneLog()
		self.filter.ignoreCommand = cmd + " bad arguments <ip>"
		self.assertFalse(self.filter.inIgnoreIPList("192.0"))
		self.assertLogged('Usage')



class LogFile(LogCaptureTestCase):

	MISSING = 'testcases/missingLogFile'

	def setUp(self):
		LogCaptureTestCase.setUp(self)

	def tearDown(self):
		LogCaptureTestCase.tearDown(self)

	def testMissingLogFiles(self):
		self.filter = FilterPoll(None)
		self.assertRaises(IOError, self.filter.addLogPath, LogFile.MISSING)

	def testDecodeLineWarn(self):
		# incomplete line (missing byte at end), warning is suppressed:
		l = "correct line\n"
		r = l.encode('utf-16le')
		self.assertEqual(FileContainer.decode_line('TESTFILE', 'utf-16le', r), l)
		self.assertEqual(FileContainer.decode_line('TESTFILE', 'utf-16le', r[0:-1]), l[0:-1])
		self.assertNotLogged('Error decoding line')
		# complete line (incorrect surrogate in the middle), warning is there:
		r = b"incorrect \xc8\x0a line\n"
		l = r.decode('utf-8', 'replace')
		self.assertEqual(FileContainer.decode_line('TESTFILE', 'utf-8', r), l)
		self.assertLogged('Error decoding line')


class LogFileFilterPoll(unittest.TestCase):

	FILENAME = os.path.join(TEST_FILES_DIR, "testcase01.log")

	def setUp(self):
		"""Call before every test case."""
		super(LogFileFilterPoll, self).setUp()
		self.filter = FilterPoll(DummyJail())
		self.filter.addLogPath(LogFileFilterPoll.FILENAME)

	def tearDown(self):
		"""Call after every test case."""
		super(LogFileFilterPoll, self).tearDown()

	#def testOpen(self):
	#	self.filter.openLogFile(LogFile.FILENAME)

	def testIsModified(self):
		self.assertTrue(self.filter.isModified(LogFileFilterPoll.FILENAME))
		self.assertFalse(self.filter.isModified(LogFileFilterPoll.FILENAME))

	def testSeekToTimeSmallFile(self):
		# speedup search using exact date pattern:
		self.filter.setDatePattern(r'^%ExY-%Exm-%Exd %ExH:%ExM:%ExS')
		fname = tempfile.mktemp(prefix='tmp_fail2ban', suffix='.log')
		time = 1417512352
		f = open(fname, 'wb')
		fc = None
		try:
			fc = FileContainer(fname, self.filter.getLogEncoding())
			fc.open()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			f.flush()
			# empty :
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 0)
			# one entry with exact time:
			f.write(b"%s [sshd] error: PAM: failure len 1\n" % _tmb(time))
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)

			# rewrite :
			f.seek(0)
			f.truncate()
			fc.close()
			fc = FileContainer(fname, self.filter.getLogEncoding())
			fc.open()
			# no time - nothing should be found :
			for i in range(10):
				f.write(b"[sshd] error: PAM: failure len 1\n")
				f.flush()
				fc.setPos(0); self.filter.seekToTime(fc, time)

			# rewrite
			f.seek(0)
			f.truncate()
			fc.close()
			fc = FileContainer(fname, self.filter.getLogEncoding())
			fc.open()
			# one entry with smaller time:
			f.write(b"%s [sshd] error: PAM: failure len 2\n" % _tmb(time - 10))
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 53)
			# two entries with smaller time:
			f.write(b"%s [sshd] error: PAM: failure len 3 2 1\n" % _tmb(time - 9))
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 110)
			# check move after end (all of time smaller):
			f.write(b"%s [sshd] error: PAM: failure\n" % _tmb(time - 1))
			f.flush()
			self.assertEqual(fc.getFileSize(), 157)
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 157)

			# still one exact line:
			f.write(b"%s [sshd] error: PAM: Authentication failure\n" % _tmb(time))
			f.write(b"%s [sshd] error: PAM: failure len 1\n" % _tmb(time))
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 157)

			# add something hereafter:
			f.write(b"%s [sshd] error: PAM: failure len 3 2 1\n" % _tmb(time + 2))
			f.write(b"%s [sshd] error: PAM: Authentication failure\n" % _tmb(time + 3))
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 157)
			# add something hereafter:
			f.write(b"%s [sshd] error: PAM: failure\n" % _tmb(time + 9))
			f.write(b"%s [sshd] error: PAM: failure len 4 3 2\n" % _tmb(time + 9))
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 157)
			# start search from current pos :
			fc.setPos(157); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 157)
			# start search from current pos :
			fc.setPos(110); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 157)

		finally:
			if fc:
				fc.close()
			_killfile(f, fname)

	def testSeekToTimeLargeFile(self):
		# speedup search using exact date pattern:
		self.filter.setDatePattern(r'^%ExY-%Exm-%Exd %ExH:%ExM:%ExS')
		fname = tempfile.mktemp(prefix='tmp_fail2ban', suffix='.log')
		time = 1417512352
		f = open(fname, 'wb')
		fc = None
		count = 1000 if unittest.F2B.fast else 10000
		try:
			fc = FileContainer(fname, self.filter.getLogEncoding())
			fc.open()
			f.seek(0)
			# variable length of file (ca 45K or 450K before and hereafter):
			# write lines with smaller as search time:
			t = time - count - 1
			for i in range(count):
				f.write(b"%s [sshd] error: PAM: failure\n" % _tmb(t))
				t += 1
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 47*count)
			# write lines with exact search time:
			for i in range(10):
				f.write(b"%s [sshd] error: PAM: failure\n" % _tmb(time))
			f.flush()
			fc.setPos(0); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 47*count)
			fc.setPos(4*count); self.filter.seekToTime(fc, time)
			self.assertEqual(fc.getPos(), 47*count)
			# write lines with greater as search time:
			t = time+1
			for i in range(count//500):
				for j in range(500):
					f.write(b"%s [sshd] error: PAM: failure\n" % _tmb(t))
					t += 1
				f.flush()
				fc.setPos(0); self.filter.seekToTime(fc, time)
				self.assertEqual(fc.getPos(), 47*count)
				fc.setPos(53); self.filter.seekToTime(fc, time)
				self.assertEqual(fc.getPos(), 47*count)
		
		finally:
			if fc:
				fc.close()
			_killfile(f, fname)

class LogFileMonitor(LogCaptureTestCase):
	"""Few more tests for FilterPoll API
	"""
	def setUp(self):
		"""Call before every test case."""
		setUpMyTime()
		LogCaptureTestCase.setUp(self)
		self.filter = self.name = 'NA'
		_, self.name = tempfile.mkstemp('fail2ban', 'monitorfailures')
		self.file = open(self.name, 'ab')
		self.filter = FilterPoll(DummyJail())
		self.filter.addLogPath(self.name, autoSeek=False)
		self.filter.active = True
		self.filter.addFailRegex(r"(?:(?:Authentication failure|Failed [-/\w+]+) for(?: [iI](?:llegal|nvalid) user)?|[Ii](?:llegal|nvalid) user|ROOT LOGIN REFUSED) .*(?: from|FROM) <HOST>")

	def tearDown(self):
		tearDownMyTime()
		LogCaptureTestCase.tearDown(self)
		_killfile(self.file, self.name)
		pass

	def isModified(self, delay=2):
		"""Wait up to `delay` sec to assure that it was modified or not
		"""
		return Utils.wait_for(lambda: self.filter.isModified(self.name), _maxWaitTime(delay))

	def notModified(self, delay=2):
		"""Wait up to `delay` sec as long as it was not modified
		"""
		return Utils.wait_for(lambda: not self.filter.isModified(self.name), _maxWaitTime(delay))

	def testUnaccessibleLogFile(self):
		os.chmod(self.name, 0)
		self.filter.getFailures(self.name)
		failure_was_logged = self._is_logged('Unable to open %s' % self.name)
		# verify that we cannot access the file. Checking by name of user is not
		# sufficient since could be a fakeroot or some other super-user
		is_root = True
		try:
			with open(self.name) as f: # pragma: no cover - normally no root
				f.read()
		except IOError:
			is_root = False

		# If ran as root, those restrictive permissions would not
		# forbid log to be read.
		self.assertTrue(failure_was_logged != is_root)

	def testNoLogFile(self):
		_killfile(self.file, self.name)
		self.filter.getFailures(self.name)
		self.assertLogged('Unable to open %s' % self.name)

	def testErrorProcessLine(self):
		# speedup search using exact date pattern:
		self.filter.setDatePattern(r'^%ExY-%Exm-%Exd %ExH:%ExM:%ExS')
		self.filter.sleeptime /= 1000.0
		## produce error with not callable processLine:
		_org_processLine = self.filter.processLine
		self.filter.processLine = None
		for i in range(100):
			self.file.write(b"line%d\n" % 1)
		self.file.flush()
		for i in range(100):
			self.filter.getFailures(self.name)
		self.assertLogged('Failed to process line:')
		self.assertLogged('Too many errors at once')
		self.pruneLog()
		self.assertTrue(self.filter.idle)
		self.filter.idle = False
		self.filter.getFailures(self.name)
		self.filter.processLine = _org_processLine
		self.file.write(b"line%d\n" % 1)
		self.file.flush()
		self.filter.getFailures(self.name)
		self.assertNotLogged('Failed to process line:')

	def testRemovingFailRegex(self):
		self.filter.delFailRegex(0)
		self.assertNotLogged('Cannot remove regular expression. Index 0 is not valid')
		self.filter.delFailRegex(0)
		self.assertLogged('Cannot remove regular expression. Index 0 is not valid')

	def testRemovingIgnoreRegex(self):
		self.filter.delIgnoreRegex(0)
		self.assertLogged('Cannot remove regular expression. Index 0 is not valid')

	def testNewChangeViaIsModified(self):
		# it is a brand new one -- so first we think it is modified
		self.assertTrue(self.isModified())
		# but not any longer
		self.assertTrue(self.notModified())
		self.assertTrue(self.notModified())
		mtimesleep()				# to guarantee freshier mtime
		for i in range(4):			  # few changes
			# unless we write into it
			self.file.write(b"line%d\n" % i)
			self.file.flush()
			self.assertTrue(self.isModified())
			self.assertTrue(self.notModified())
			mtimesleep()				# to guarantee freshier mtime
		os.rename(self.name, self.name + '.old')
		# we are not signaling as modified whenever
		# it gets away
		self.assertTrue(self.notModified(1))
		f = open(self.name, 'ab')
		self.assertTrue(self.isModified())
		self.assertTrue(self.notModified())
		mtimesleep()
		f.write(b"line%d\n" % i)
		f.flush()
		self.assertTrue(self.isModified())
		self.assertTrue(self.notModified())
		_killfile(f, self.name)
		_killfile(self.name, self.name + '.old')
		pass

	def testNewChangeViaGetFailures_simple(self):
		# speedup search using exact date pattern:
		self.filter.setDatePattern(r'^(?:%a )?%b %d %H:%M:%S(?:\.%f)?(?: %ExY)?')
		# suck in lines from this sample log file
		self.filter.getFailures(self.name)
		self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)

		# Now let's feed it with entries from the file
		_copy_lines_between_files(GetFailures.FILENAME_01, self.file, n=5)
		self.filter.getFailures(self.name)
		self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
		# and it should have not been enough

		_copy_lines_between_files(GetFailures.FILENAME_01, self.file, skip=12, n=3)
		self.filter.getFailures(self.name)
		_assert_correct_last_attempt(self, self.filter, GetFailures.FAILURES_01)

	def testNewChangeViaGetFailures_rewrite(self):
		# speedup search using exact date pattern:
		self.filter.setDatePattern(r'^(?:%a )?%b %d %H:%M:%S(?:\.%f)?(?: %ExY)?')
		#
		# if we rewrite the file at once
		self.file.close()
		_copy_lines_between_files(GetFailures.FILENAME_01, self.name).close()
		self.filter.getFailures(self.name)
		_assert_correct_last_attempt(self, self.filter, GetFailures.FAILURES_01)

		# What if file gets overridden
		# yoh: skip so we skip those 2 identical lines which our
		# filter "marked" as the known beginning, otherwise it
		# would not detect "rotation"
		self.file = _copy_lines_between_files(GetFailures.FILENAME_01, self.name,
											  skip=12, n=3, mode='w')
		self.filter.getFailures(self.name)
		#self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
		_assert_correct_last_attempt(self, self.filter, GetFailures.FAILURES_01)

	def testNewChangeViaGetFailures_move(self):
		# speedup search using exact date pattern:
		self.filter.setDatePattern(r'^(?:%a )?%b %d %H:%M:%S(?:\.%f)?(?: %ExY)?')
		#
		# if we move file into a new location while it has been open already
		self.file.close()
		self.file = _copy_lines_between_files(GetFailures.FILENAME_01, self.name,
											  n=14, mode='w')
		self.filter.getFailures(self.name)
		self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
		self.assertEqual(self.filter.failManager.getFailTotal(), 2)

		# move aside, but leaving the handle still open...
		os.rename(self.name, self.name + '.bak')
		_copy_lines_between_files(GetFailures.FILENAME_01, self.name, skip=14, n=1).close()
		self.filter.getFailures(self.name)
		#_assert_correct_last_attempt(self, self.filter, GetFailures.FAILURES_01)
		self.assertEqual(self.filter.failManager.getFailTotal(), 3)


class CommonMonitorTestCase(unittest.TestCase):

	def setUp(self):
		"""Call before every test case."""
		super(CommonMonitorTestCase, self).setUp()
		self._failTotal = 0

	def tearDown(self):
		super(CommonMonitorTestCase, self).tearDown()
		self.assertFalse(hasattr(self, "_unexpectedError"))

	def waitFailTotal(self, count, delay=1):
		"""Wait up to `delay` sec to assure that expected failure `count` reached
		"""
		ret = Utils.wait_for(
			lambda: self.filter.failManager.getFailTotal() >= (self._failTotal + count) and self.jail.isFilled(),
			_maxWaitTime(delay))
		self._failTotal += count
		return ret

	def isFilled(self, delay=1):
		"""Wait up to `delay` sec to assure that it was modified or not
		"""
		return Utils.wait_for(self.jail.isFilled, _maxWaitTime(delay))

	def isEmpty(self, delay=5):
		"""Wait up to `delay` sec to assure that it empty again
		"""
		return Utils.wait_for(self.jail.isEmpty, _maxWaitTime(delay))

	def waitForTicks(self, ticks, delay=2):
		"""Wait up to `delay` sec to assure that it was modified or not
		"""
		last_ticks = self.filter.ticks
		return Utils.wait_for(lambda: self.filter.ticks >= last_ticks + ticks, _maxWaitTime(delay))

	def commonFltError(self, reason="common", exc=None):
		""" Mock-up for default common error handler to find caught unhandled exceptions
		could occur in filters
		"""
		self._commonFltError(reason, exc)
		if reason == "unhandled":
			DefLogSys.critical("Caught unhandled exception in main cycle of %r : %r", self.filter, exc, exc_info=True)
			self._unexpectedError = True
		# self.assertNotEqual(reason, "unhandled")


def get_monitor_failures_testcase(Filter_):
	"""Generator of TestCase's for different filters/backends
	"""

	# add Filter_'s name so we could easily identify bad cows
	testclass_name = tempfile.mktemp(
		'fail2ban', 'monitorfailures_%s_' % (Filter_.__name__,))

	class MonitorFailures(CommonMonitorTestCase):
		count = 0

		def setUp(self):
			"""Call before every test case."""
			super(MonitorFailures, self).setUp()
			setUpMyTime()
			self.filter = self.name = 'NA'
			self.name = '%s-%d' % (testclass_name, self.count)
			MonitorFailures.count += 1 # so we have unique filenames across tests
			self.file = open(self.name, 'ab')
			self.jail = DummyJail()
			self.filter = Filter_(self.jail)
			# mock-up common error to find caught unhandled exceptions:
			self._commonFltError, self.filter.commonError = self.filter.commonError, self.commonFltError
			self.filter.addLogPath(self.name, autoSeek=False)
			# speedup search using exact date pattern:
			self.filter.setDatePattern(r'^(?:%a )?%b %d %H:%M:%S(?:\.%f)?(?: %ExY)?')
			self.filter.active = True
			self.filter.addFailRegex(r"(?:(?:Authentication failure|Failed [-/\w+]+) for(?: [iI](?:llegal|nvalid) user)?|[Ii](?:llegal|nvalid) user|ROOT LOGIN REFUSED) .*(?: from|FROM) <HOST>")
			self.filter.start()
			# If filter is polling it would sleep a bit to guarantee that
			# we have initial time-stamp difference to trigger "actions"
			self._sleep_4_poll()
			#print "D: started filter %s" % self.filter

		def tearDown(self):
			tearDownMyTime()
			#print "D: SLEEPING A BIT"
			#import time; time.sleep(5)
			#print "D: TEARING DOWN"
			self.filter.stop()
			#print "D: WAITING FOR FILTER TO STOP"
			self.filter.join()		  # wait for the thread to terminate
			#print "D: KILLING THE FILE"
			_killfile(self.file, self.name)
			#time.sleep(0.2)			  # Give FS time to ack the removal
			super(MonitorFailures, self).tearDown()

		def _sleep_4_poll(self):
			# Since FilterPoll relies on time stamps and some
			# actions might be happening too fast in the tests,
			# sleep a bit to guarantee reliable time stamps
			if isinstance(self.filter, FilterPoll):
				Utils.wait_for(self.filter.isAlive, _maxWaitTime(5))

		def assert_correct_last_attempt(self, failures, count=None):
			self.assertTrue(self.waitFailTotal(count if count else failures[1], 10))
			_assert_correct_last_attempt(self, self.jail, failures, count=count)

		def test_grow_file(self):
			self._test_grow_file()

		def test_grow_file_in_idle(self):
			self._test_grow_file(True)

		def _test_grow_file(self, idle=False):
			if idle:
				self.filter.sleeptime /= 100.0
				self.filter.idle = True
				self.waitForTicks(1)
			# suck in lines from this sample log file
			self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)

			# Now let's feed it with entries from the file
			_copy_lines_between_files(GetFailures.FILENAME_01, self.file, n=12)
			self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
			# and our dummy jail is empty as well
			self.assertFalse(len(self.jail))
			# since it should have not been enough

			_copy_lines_between_files(GetFailures.FILENAME_01, self.file, skip=12, n=3)
			if idle:
				self.waitForTicks(1)
				self.assertTrue(self.isEmpty(1))
				return
			self.assertTrue(self.isFilled(10))
			# so we sleep a bit for it not to become empty,
			# and meanwhile pass to other thread(s) and filter should
			# have gathered new failures and passed them into the
			# DummyJail
			self.assertEqual(len(self.jail), 1)
			# and there should be no "stuck" ticket in failManager
			self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)
			self.assertEqual(len(self.jail), 0)

			#return
			# just for fun let's copy all of them again and see if that results
			# in a new ban
			_copy_lines_between_files(GetFailures.FILENAME_01, self.file, skip=12, n=3)
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)

		def test_rewrite_file(self):
			# if we rewrite the file at once
			self.file.close()
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name).close()
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)

			# What if file gets overridden
			# yoh: skip so we skip those 2 identical lines which our
			# filter "marked" as the known beginning, otherwise it
			# would not detect "rotation"
			self.file = _copy_lines_between_files(GetFailures.FILENAME_01, self.name,
												  skip=12, n=3, mode='w')
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)

		def _wait4failures(self, count=2, waitEmpty=True):
			# Poll might need more time
			if waitEmpty:
				self.assertTrue(self.isEmpty(_maxWaitTime(5)),
								"Queue must be empty but it is not: %s."
								% (', '.join([str(x) for x in self.jail.queue])))
				self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
			Utils.wait_for(lambda: self.filter.failManager.getFailTotal() >= count, _maxWaitTime(10))
			self.assertEqual(self.filter.failManager.getFailTotal(), count)

		def test_move_file(self):
			# if we move file into a new location while it has been open already
			self.file.close()
			self.file = _copy_lines_between_files(GetFailures.FILENAME_01, self.name,
												  n=14, mode='w')
			self._wait4failures()

			# move aside, but leaving the handle still open...
			os.rename(self.name, self.name + '.bak')
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name, skip=14, n=1,
				lines=["Aug 14 11:59:59 [logrotate] rotation 1"]).close()
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)
			self.assertEqual(self.filter.failManager.getFailTotal(), 3)

			# now remove the moved file
			_killfile(None, self.name + '.bak')
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name, skip=12, n=3,
				lines=["Aug 14 11:59:59 [logrotate] rotation 2"]).close()
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)
			self.assertEqual(self.filter.failManager.getFailTotal(), 6)

		def test_pyinotify_delWatch(self):
			if hasattr(self.filter, '_delWatch'): # pyinotify only
				m = self.filter._FilterPyinotify__monitor
				# remove existing watch:
				self.assertTrue(self.filter._delWatch(m.get_wd(self.name)))
				# mockup get_path to allow once find path for invalid wd-value:
				_org_get_path = m.get_path
				def _get_path(wd):
					#m.get_path = _org_get_path
					return 'test'
				m.get_path = _get_path
				# try remove watch using definitely not existing handle:
				self.assertFalse(self.filter._delWatch(0x7fffffff))
				m.get_path = _org_get_path

		def test_del_file(self):
			# test filter reaction by delete watching file:
			self.file.close()
			self.waitForTicks(1)
			# remove file (cause detection of log-rotation)...
			os.unlink(self.name)
			# check it was detected (in pending files):
			self.waitForTicks(2)
			if hasattr(self.filter, "getPendingPaths"):
				self.assertTrue(Utils.wait_for(lambda: self.name in self.filter.getPendingPaths(), _maxWaitTime(10)))
				self.assertEqual(len(self.filter.getPendingPaths()), 1)

		@with_tmpdir
		def test_move_dir(self, tmp):
			self.file.close()
			self.filter.setMaxRetry(10)
			self.filter.delLogPath(self.name)
			_killfile(None, self.name)
			# if we rename parent dir into a new location (simulate directory-base log rotation)
			tmpsub1 = os.path.join(tmp, "1")
			tmpsub2 = os.path.join(tmp, "2")
			os.mkdir(tmpsub1)
			self.name = os.path.join(tmpsub1, os.path.basename(self.name))
			os.close(os.open(self.name, os.O_CREAT|os.O_APPEND)); # create empty file
			self.filter.addLogPath(self.name, autoSeek=False)
			
			self.file = _copy_lines_between_files(GetFailures.FILENAME_01, self.name,
												  skip=12, n=1, mode='w')
			self.file.close()
			self._wait4failures(1)

			# rotate whole directory: rename directory 1 as 2a:
			os.rename(tmpsub1, tmpsub2 + 'a')
			os.mkdir(tmpsub1)
			self.file = _copy_lines_between_files(GetFailures.FILENAME_01, self.name,
												  skip=12, n=1, mode='w', lines=["Aug 14 11:59:59 [logrotate] rotation 1"])
			self.file.close()
			self._wait4failures(2)

			# rotate whole directory: rename directory 1 as 2b:
			os.rename(tmpsub1, tmpsub2 + 'b')
			# wait a bit in-between (try to increase coverage, should find pending file for pending dir):
			self.waitForTicks(2)
			os.mkdir(tmpsub1)
			self.waitForTicks(2)
			self.file = _copy_lines_between_files(GetFailures.FILENAME_01, self.name,
												  skip=12, n=1, mode='w', lines=["Aug 14 11:59:59 [logrotate] rotation 2"])
			self.file.close()
			self._wait4failures(3)

			# stop before tmpdir deleted (just prevents many monitor events)
			self.filter.stop()
			self.filter.join()


		def _test_move_into_file(self, interim_kill=False):
			# if we move a new file into the location of an old (monitored) file
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name).close()
			# make sure that it is monitored first
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)
			self.assertEqual(self.filter.failManager.getFailTotal(), 3)

			if interim_kill:
				_killfile(None, self.name)
				time.sleep(Utils.DEFAULT_SHORT_INTERVAL)				  # let them know

			# now create a new one to override old one
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name + '.new',
				skip=12, n=3).close()
			os.rename(self.name + '.new', self.name)
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)
			self.assertEqual(self.filter.failManager.getFailTotal(), 6)

			# and to make sure that it now monitored for changes
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name,
				skip=12, n=3).close()
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)
			self.assertEqual(self.filter.failManager.getFailTotal(), 9)

		def test_move_into_file(self):
			self._test_move_into_file(interim_kill=False)

		def test_move_into_file_after_removed(self):
			# exactly as above test + remove file explicitly
			# to test against possible drop-out of the file from monitoring
		    self._test_move_into_file(interim_kill=True)

		def test_new_bogus_file(self):
			# to make sure that watching whole directory does not effect
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name, n=100).close()
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)

			# create a bogus file in the same directory and see if that doesn't affect
			open(self.name + '.bak2', 'w').close()
			_copy_lines_between_files(GetFailures.FILENAME_01, self.name, skip=12, n=3).close()
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)
			self.assertEqual(self.filter.failManager.getFailTotal(), 6)
			_killfile(None, self.name + '.bak2')

		def test_delLogPath(self):
			# Smoke test for removing of the path from being watched

			# basic full test
			_copy_lines_between_files(GetFailures.FILENAME_01, self.file, n=100)
			self.assert_correct_last_attempt(GetFailures.FAILURES_01)

			# and now remove the LogPath
			self.filter.delLogPath(self.name)
			# wait a bit for filter (backend-threads):
			self.waitForTicks(2)

			_copy_lines_between_files(GetFailures.FILENAME_01, self.file, n=100)
			# so we should get no more failures detected
			self.assertTrue(self.isEmpty(10))

			# but then if we add it back again (no seek to time in FileFilter's, because in file used the same time)
			self.filter.addLogPath(self.name, autoSeek=False)
			# wait a bit for filter (backend-threads):
			self.waitForTicks(2)
			# Tricky catch here is that it should get them from the
			# tail written before, so let's not copy anything yet
			#_copy_lines_between_files(GetFailures.FILENAME_01, self.name, n=100)
			# we should detect the failures
			self.assert_correct_last_attempt(GetFailures.FAILURES_01, count=3) # was needed if we write twice above

			# now copy and get even more
			_copy_lines_between_files(GetFailures.FILENAME_01, self.file, skip=12, n=3)
			# check for 3 failures (not 9), because 6 already get above...
			self.assert_correct_last_attempt(GetFailures.FAILURES_01, count=3)
			# total count in this test:
			self._wait4failures(12, False)

	cls = MonitorFailures
	cls.__qualname__ = cls.__name__ = "MonitorFailures<%s>(%s)" \
			  % (Filter_.__name__, testclass_name) # 'tempfile')
	return cls


def get_monitor_failures_journal_testcase(Filter_): # pragma: systemd no cover
	"""Generator of TestCase's for journal based filters/backends
	"""
	
	testclass_name = "monitorjournalfailures_%s" % (Filter_.__name__,)

	class MonitorJournalFailures(CommonMonitorTestCase):
		def setUp(self):
			"""Call before every test case."""
			super(MonitorJournalFailures, self).setUp()
			self.test_file = os.path.join(TEST_FILES_DIR, "testcase-journal.log")
			self.jail = DummyJail()
			self.filter = None
			# UUID used to ensure that only meeages generated
			# as part of this test are picked up by the filter
			self.test_uuid = str(uuid.uuid4())
			self.name = "%s-%s" % (testclass_name, self.test_uuid)
			self.journal_fields = {
				'TEST_FIELD': "1", 'TEST_UUID': self.test_uuid}

		def _initFilter(self, **kwargs):
			self._getRuntimeJournal() # check journal available
			self.filter = Filter_(self.jail, **kwargs)
			# mock-up common error to find caught unhandled exceptions:
			self._commonFltError, self.filter.commonError = self.filter.commonError, self.commonFltError
			self.filter.addJournalMatch([
				"SYSLOG_IDENTIFIER=fail2ban-testcases",
				"TEST_FIELD=1",
				"TEST_UUID=%s" % self.test_uuid])
			self.filter.addJournalMatch([
				"SYSLOG_IDENTIFIER=fail2ban-testcases",
				"TEST_FIELD=2",
				"TEST_UUID=%s" % self.test_uuid])
			self.filter.addFailRegex(r"(?:(?:Authentication failure|Failed [-/\w+]+) for(?: [iI](?:llegal|nvalid) user)?|[Ii](?:llegal|nvalid) user|ROOT LOGIN REFUSED) .*(?: from|FROM) <HOST>")

		def tearDown(self):
			if self.filter and (self.filter.active or self.filter.active is None):
				self.filter.stop()
				self.filter.join()		  # wait for the thread to terminate
			super(MonitorJournalFailures, self).tearDown()

		def _getRuntimeJournal(self):
			"""Retrieve current system journal path

			If not found, SkipTest exception will be raised.
			"""
			# we can cache it:
			if not hasattr(MonitorJournalFailures, "_runtimeJournal"):
				# Depending on the system, it could be found under /run or /var/log (e.g. Debian)
				# which are pointed by different systemd-path variables.  We will
				# check one at at time until the first hit
				for systemd_var in 'system-runtime-logs', 'system-state-logs':
					tmp = Utils.executeCmd(
						'find "$(systemd-path %s)/journal" -name system.journal -readable' % systemd_var,
						timeout=10, shell=True, output=True
					)
					self.assertTrue(tmp)
					out = str(tmp[1].decode('utf-8')).split('\n')[0]
					if out: break
				# additional check appropriate default settings (if not root/sudoer and not already set):
				if os.geteuid() != 0 and os.getenv("F2B_SYSTEMD_DEFAULT_FLAGS", None) is None:
					# filter default SYSTEM_ONLY(4) is hardly usable for not root/sudoer tester,
					# so back to default LOCAL_ONLY(1):
					os.environ["F2B_SYSTEMD_DEFAULT_FLAGS"] = "0"; # or "1", what will be similar to journalflags=0 or ...=1
				MonitorJournalFailures._runtimeJournal = out
			if MonitorJournalFailures._runtimeJournal:
				return MonitorJournalFailures._runtimeJournal
			raise unittest.SkipTest('systemd journal seems to be not available (e. g. no rights to read)')
		
		def testGlobJournal_System(self):
			if not journal: # pragma: no cover
				raise unittest.SkipTest("systemd python interface not available")
			jrnlfile = self._getRuntimeJournal()
			jrnlpath = os.path.dirname(jrnlfile)
			self.assertIn(jrnlfile, _globJournalFiles(journal.SYSTEM_ONLY))
			self.assertIn(jrnlfile, _globJournalFiles(journal.SYSTEM_ONLY, jrnlpath))
			self.assertIn(jrnlfile, _globJournalFiles(journal.LOCAL_ONLY))
			self.assertIn(jrnlfile, _globJournalFiles(journal.LOCAL_ONLY, jrnlpath))

		@with_tmpdir
		def testGlobJournal(self, tmp):
			if not journal: # pragma: no cover
				raise unittest.SkipTest("systemd python interface not available")
			# no files yet in temp-path:
			self.assertFalse(_globJournalFiles(None, tmp))
			# test against temp-path, shall ignore all rotated files:
			tsysjrnl = os.path.join(tmp, 'system.journal')
			tusrjrnl = os.path.join(tmp, 'user-%s.journal' % os.getuid())
			def touch(fn): os.close(os.open(fn, os.O_CREAT|os.O_APPEND))
			touch(tsysjrnl);
			touch(tusrjrnl);
			touch(os.path.join(tmp, 'system@test-rotated.journal'));
			touch(os.path.join(tmp, 'user-%s@test-rotated.journal' % os.getuid()));
			self.assertSortedEqual(_globJournalFiles(None, tmp), {tsysjrnl, tusrjrnl})
			self.assertSortedEqual(_globJournalFiles(journal.SYSTEM_ONLY, tmp), {tsysjrnl})
			self.assertSortedEqual(_globJournalFiles(journal.CURRENT_USER, tmp), {tusrjrnl})

		def testJournalFilesArg(self):
			# retrieve current system journal path
			jrnlfile = self._getRuntimeJournal()
			self._initFilter(journalfiles=jrnlfile)

		def testJournalFilesAndFlagsArgs(self):
			# retrieve current system journal path
			jrnlfile = self._getRuntimeJournal()
			self._initFilter(journalfiles=jrnlfile, journalflags=0)

		def testJournalPathArg(self):
			# retrieve current system journal path
			jrnlpath = self._getRuntimeJournal()
			jrnlpath = os.path.dirname(jrnlpath)
			self._initFilter(journalpath=jrnlpath)
			self.filter.seekToTime(
				datetime.datetime.now() - datetime.timedelta(days=1)
			)
			self.filter.start()
			self.waitForTicks(2)
			self.assertTrue(self.isEmpty(1))
			self.assertEqual(len(self.jail), 0)
			self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
		def testJournalPath_RotatedArg(self):
			# retrieve current system journal path
			jrnlpath = self._getRuntimeJournal()
			jrnlpath = os.path.dirname(jrnlpath)
			self._initFilter(journalpath=jrnlpath, rotated=1)

		def testJournalFlagsArg(self):
			self._initFilter(journalflags=0)
			self._initFilter(journalflags=1)
		def testJournalFlags_RotatedArg(self):
			self._initFilter(journalflags=0, rotated=1)
			self._initFilter(journalflags=1, rotated=1)

		def assert_correct_ban(self, test_ip, test_attempts):
			self.assertTrue(self.waitFailTotal(test_attempts, 10)) # give Filter a chance to react
			ticket = self.jail.getFailTicket()
			self.assertTrue(ticket)

			attempts = ticket.getAttempt()
			ip = ticket.getID()
			ticket.getMatches()

			self.assertEqual(ip, test_ip)
			self.assertEqual(attempts, test_attempts)

		def test_grow_file(self):
			self._test_grow_file()

		def test_grow_file_in_idle(self):
			self._test_grow_file(True)

		def _test_grow_file(self, idle=False):
			self._initFilter()
			self.filter.start()
			if idle:
				self.filter.sleeptime /= 100.0
				self.filter.idle = True
			self.waitForTicks(1)
			self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)

			# Now let's feed it with entries from the file
			_copy_lines_to_journal(
				self.test_file, self.journal_fields, n=2)
			self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
			# and our dummy jail is empty as well
			self.assertFalse(len(self.jail))
			# since it should have not been enough

			_copy_lines_to_journal(
				self.test_file, self.journal_fields, skip=2, n=3)
			if idle:
				self.waitForTicks(1)
				self.assertTrue(self.isEmpty(1))
				return
			self.assertTrue(self.isFilled(10))
			# so we sleep for up to 6 sec for it not to become empty,
			# and meanwhile pass to other thread(s) and filter should
			# have gathered new failures and passed them into the
			# DummyJail
			self.assertEqual(len(self.jail), 1)
			# and there should be no "stuck" ticket in failManager
			self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)
			self.assert_correct_ban("193.168.0.128", 3)
			self.assertEqual(len(self.jail), 0)

			# Lets read some more to check it bans again
			_copy_lines_to_journal(
				self.test_file, self.journal_fields, skip=5, n=4)
			self.assert_correct_ban("193.168.0.128", 3)

		@with_alt_time
		def test_grow_file_with_db(self):

			def _gen_failure(ip):
				# insert new failures ans check it is monitored:
				fields = self.journal_fields
				fields.update(TEST_JOURNAL_FIELDS)
				journal.send(MESSAGE="error: PAM: Authentication failure for test from "+ip, **fields)
				self.waitForTicks(1)
				self.assert_correct_ban(ip, 1)

			# coverage for update log:
			self.jail.database = getFail2BanDb(':memory:')
			self.jail.database.addJail(self.jail)
			MyTime.setTime(time.time())
			self._test_grow_file()
			# stop:
			self.filter.stop()
			self.filter.join()
			MyTime.setTime(time.time() + 10)
			# update log manually (should cause a seek to end of log without wait for next second):
			self.jail.database.updateJournal(self.jail, 'systemd-journal', MyTime.time(), 'TEST')
			# check seek to last (simulated) position succeeds (without bans of previous copied tickets):
			self._failTotal = 0
			self._initFilter()
			self.filter.setMaxRetry(1)
			self.filter.start()
			self.waitForTicks(2)
			# check new IP but no old IPs found:
			_gen_failure("192.0.2.5")
			self.assertFalse(self.jail.getFailTicket())

			# now the same with increased time (check now - findtime case):
			self.filter.stop()
			self.filter.join()
			MyTime.setTime(time.time() + 10000)
			self._failTotal = 0
			self._initFilter()
			self.filter.setMaxRetry(1)
			self.filter.start()
			self.waitForTicks(2)
			MyTime.setTime(time.time() + 20)
			# check new IP but no old IPs found:
			_gen_failure("192.0.2.6")
			self.assertFalse(self.jail.getFailTicket())

			# now reset DB, so we'd find all messages before filter entering in operation mode:
			self.filter.stop()
			self.filter.join()
			self.jail.database.updateJournal(self.jail, 'systemd-journal', MyTime.time()-10000, 'TEST')
			self._initFilter()
			self.filter.setMaxRetry(1)
			states = []
			def _state(*args):
				try:
					self.assertNotIn("** in operation", states)
					self.assertFalse(self.filter.inOperation)
					states.append("** process line: %r" % (args,))
				except Exception as e:
					states.append("** failed: %r" % (e,))
					raise
			self.filter.processLineAndAdd = _state
			def _inoper():
				try:
					self.assertNotIn("** in operation", states)
					self.assertEqual(len(states), 11)
					states.append("** in operation")
					self.filter.__class__.inOperationMode(self.filter)
				except Exception as e:
					states.append("** failed: %r" % (e,))
					raise
			self.filter.inOperationMode = _inoper
			self.filter.start()
			self.waitForTicks(12)
			self.assertTrue(Utils.wait_for(lambda: len(states) == 12, _maxWaitTime(10)))
			self.assertEqual(states[-1], "** in operation")

		def test_delJournalMatch(self):
			self._initFilter()
			self.filter.start()
			self.waitForTicks(1); # wait for start
			# Smoke test for removing of match

			# basic full test
			_copy_lines_to_journal(
				self.test_file, self.journal_fields, n=5)
			self.assert_correct_ban("193.168.0.128", 3)

			# and now remove the JournalMatch
			self.filter.delJournalMatch([
				"SYSLOG_IDENTIFIER=fail2ban-testcases",
				"TEST_FIELD=1",
				"TEST_UUID=%s" % self.test_uuid])

			_copy_lines_to_journal(
				self.test_file, self.journal_fields, n=5, skip=5)
			# so we should get no more failures detected
			self.assertTrue(self.isEmpty(10))

			# but then if we add it back again
			self.filter.addJournalMatch([
				"SYSLOG_IDENTIFIER=fail2ban-testcases",
				"TEST_FIELD=1",
				"TEST_UUID=%s" % self.test_uuid])
			self.assert_correct_ban("193.168.0.128", 3)
			_copy_lines_to_journal(
				self.test_file, self.journal_fields, n=6, skip=10)
			# we should detect the failures
			self.assertTrue(self.isFilled(10))

		def test_WrongChar(self):
			self._initFilter()
			self.filter.start()
			self.waitForTicks(1); # wait for start
			# Now let's feed it with entries from the file
			_copy_lines_to_journal(
				self.test_file, self.journal_fields, skip=15, n=4)
			self.waitForTicks(1)
			self.assertTrue(self.isFilled(10))
			self.assert_correct_ban("87.142.124.10", 3)
			# Add direct utf, unicode, blob:
			for l in (
		    "error: PAM: Authentication failure for \xe4\xf6\xfc\xdf from 192.0.2.1",
		   "error: PAM: Authentication failure for \xe4\xf6\xfc\xdf from 192.0.2.1",
		   b"error: PAM: Authentication failure for \xe4\xf6\xfc\xdf from 192.0.2.1".decode('utf-8', 'replace'),
		    "error: PAM: Authentication failure for \xc3\xa4\xc3\xb6\xc3\xbc\xc3\x9f from 192.0.2.2",
		   "error: PAM: Authentication failure for \xc3\xa4\xc3\xb6\xc3\xbc\xc3\x9f from 192.0.2.2",
		   b"error: PAM: Authentication failure for \xc3\xa4\xc3\xb6\xc3\xbc\xc3\x9f from 192.0.2.2".decode('utf-8', 'replace')
			):
				fields = self.journal_fields
				fields.update(TEST_JOURNAL_FIELDS)
				journal.send(MESSAGE=l, **fields)
			self.waitForTicks(1)
			self.waitFailTotal(6, 10)
			self.assertTrue(Utils.wait_for(lambda: len(self.jail) == 2, 10))
			self.assertSortedEqual([self.jail.getFailTicket().getID(), self.jail.getFailTicket().getID()], 
				["192.0.2.1", "192.0.2.2"])

	cls = MonitorJournalFailures
	cls.__qualname__ = cls.__name__ = "MonitorJournalFailures<%s>(%s)" \
			  % (Filter_.__name__, testclass_name)
	return cls


class GetFailures(LogCaptureTestCase):

	FILENAME_01 = os.path.join(TEST_FILES_DIR, "testcase01.log")
	FILENAME_02 = os.path.join(TEST_FILES_DIR, "testcase02.log")
	FILENAME_03 = os.path.join(TEST_FILES_DIR, "testcase03.log")
	FILENAME_04 = os.path.join(TEST_FILES_DIR, "testcase04.log")
	FILENAME_USEDNS = os.path.join(TEST_FILES_DIR, "testcase-usedns.log")
	FILENAME_MULTILINE = os.path.join(TEST_FILES_DIR, "testcase-multiline.log")

	# so that they could be reused by other tests
	FAILURES_01 = ('193.168.0.128', 3, 1124013599.0,
				  ['Aug 14 11:59:59 [sshd] error: PAM: Authentication failure for kevin from 193.168.0.128']*3)

	def setUp(self):
		"""Call before every test case."""
		LogCaptureTestCase.setUp(self)
		setUpMyTime()
		self.jail = DummyJail()
		self.filter = FileFilter(self.jail)
		self.filter.active = True
		# speedup search using exact date pattern:
		self.filter.setDatePattern(r'^(?:%a )?%b %d %H:%M:%S(?:\.%f)?(?: %ExY)?')
		# TODO Test this
		#self.filter.setTimeRegex("\S{3}\s{1,2}\d{1,2} \d{2}:\d{2}:\d{2}")
		#self.filter.setTimePattern("%b %d %H:%M:%S")

	def tearDown(self):
		"""Call after every test case."""
		tearDownMyTime()
		LogCaptureTestCase.tearDown(self)

	def testFilterAPI(self):
		self.assertEqual(self.filter.getLogs(), [])
		self.assertEqual(self.filter.getLogCount(), 0)
		self.filter.addLogPath(GetFailures.FILENAME_01, tail=True)
		self.assertEqual(self.filter.getLogCount(), 1)
		self.assertEqual(self.filter.getLogPaths(), [GetFailures.FILENAME_01])
		self.filter.addLogPath(GetFailures.FILENAME_02, tail=True)
		self.assertEqual(self.filter.getLogCount(), 2)
		self.assertSortedEqual(self.filter.getLogPaths(), [GetFailures.FILENAME_01, GetFailures.FILENAME_02])

	def testTail(self):
		# There must be no containers registered, otherwise [-1] indexing would be wrong
		self.assertEqual(self.filter.getLogs(), [])
		self.filter.addLogPath(GetFailures.FILENAME_01, tail=True)
		self.assertEqual(self.filter.getLogs()[-1].getPos(), 1653)
		self.filter.getLogs()[-1].close()
		self.assertEqual(self.filter.getLogs()[-1].readline(), "")
		self.filter.delLogPath(GetFailures.FILENAME_01)
		self.assertEqual(self.filter.getLogs(), [])

	def testNoLogAdded(self):
		self.filter.addLogPath(GetFailures.FILENAME_01, tail=True)
		self.assertTrue(self.filter.containsLogPath(GetFailures.FILENAME_01))
		self.filter.delLogPath(GetFailures.FILENAME_01)
		self.assertFalse(self.filter.containsLogPath(GetFailures.FILENAME_01))
		# and unknown (safety and cover)
		self.assertFalse(self.filter.containsLogPath('unknown.log'))
		self.filter.delLogPath('unknown.log')


	def testGetFailures01(self, filename=None, failures=None):
		filename = filename or GetFailures.FILENAME_01
		failures = failures or GetFailures.FAILURES_01

		self.filter.addLogPath(filename, autoSeek=0)
		self.filter.addFailRegex(r"(?:(?:Authentication failure|Failed [-/\w+]+) for(?: [iI](?:llegal|nvalid) user)?|[Ii](?:llegal|nvalid) user|ROOT LOGIN REFUSED) .*(?: from|FROM) <HOST>$")
		self.filter.getFailures(filename)
		_assert_correct_last_attempt(self, self.filter,  failures)

	def testCRLFFailures01(self):
		# We first adjust logfile/failures to end with CR+LF
		fname = tempfile.mktemp(prefix='tmp_fail2ban', suffix='crlf')
		try:
			# poor man unix2dos:
			fin, fout = open(GetFailures.FILENAME_01, 'rb'), open(fname, 'wb')
			for l in fin.read().splitlines():
				fout.write(l + b'\r\n')
			fin.close()
			fout.close()

			# now see if we should be getting the "same" failures
			self.testGetFailures01(filename=fname)
		finally:
			_killfile(fout, fname)

	def testNLCharAsPartOfUniChar(self):
		fname = tempfile.mktemp(prefix='tmp_fail2ban', suffix='uni')
		# test two multi-byte encodings (both contains `\x0A` in either \x02\x0A or \x0A\x02):
		for enc in ('utf-16be', 'utf-16le'):
			self.pruneLog("[test-phase encoding=%s]" % enc)
			try:
				fout = open(fname, 'wb')
				tm = int(time.time())
				# test on unicode string containing \x0A as part of uni-char,
				# it must produce exactly 2 lines (both are failures):
				for l in (
					'%s \u20AC Failed auth: invalid user Test\u020A from 192.0.2.1\n' % tm,
					'%s \u20AC Failed auth: invalid user TestI from 192.0.2.2\n' % tm
				):
					fout.write(l.encode(enc))
				fout.close()

				self.filter.setLogEncoding(enc)
				self.filter.addLogPath(fname, autoSeek=0)
				self.filter.setDatePattern((r'^EPOCH',))
				self.filter.addFailRegex(r"Failed .* from <HOST>")
				self.filter.getFailures(fname)
				self.assertLogged(
					"[DummyJail] Found 192.0.2.1",
					"[DummyJail] Found 192.0.2.2", all=True, wait=True)
			finally:
				_killfile(fout, fname)
				self.filter.delLogPath(fname)
		# must find 4 failures and generate 2 tickets (2 IPs with each 2 failures):
		self.assertEqual(self.filter.failManager.getFailCount(), (2, 4))

	def testGetFailures02(self):
		output = ('141.3.81.106', 4, 1124013539.0,
				  ['Aug 14 11:%d:59 i60p295 sshd[12365]: Failed publickey for roehl from ::ffff:141.3.81.106 port 51332 ssh2'
				   % m for m in (53, 54, 57, 58)])

		self.filter.setMaxRetry(4)
		self.filter.addLogPath(GetFailures.FILENAME_02, autoSeek=0)
		self.filter.addFailRegex(r"Failed .* from <HOST>")
		self.filter.getFailures(GetFailures.FILENAME_02)
		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailures03(self):
		output = ('203.162.223.135', 6, 1124013600.0)

		self.filter.setMaxRetry(6)
		self.filter.addLogPath(GetFailures.FILENAME_03, autoSeek=0)
		self.filter.addFailRegex(r"error,relay=<HOST>,.*550 User unknown")
		self.filter.getFailures(GetFailures.FILENAME_03)
		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailures03_InOperation(self):
		output = ('203.162.223.135', 9, 1124013600.0)

		self.filter.setMaxRetry(9)
		self.filter.addLogPath(GetFailures.FILENAME_03, autoSeek=0)
		self.filter.addFailRegex(r"error,relay=<HOST>,.*550 User unknown")
		self.filter.getFailures(GetFailures.FILENAME_03, inOperation=True)
		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailures03_Seek1(self):
		# same test as above but with seek to 'Aug 14 11:55:04' - so other output ...
		output = ('203.162.223.135', 3, 1124013600.0)

		self.filter.addLogPath(GetFailures.FILENAME_03, autoSeek=output[2] - 4*60)
		self.filter.addFailRegex(r"error,relay=<HOST>,.*550 User unknown")
		self.filter.getFailures(GetFailures.FILENAME_03)
		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailures03_Seek2(self):
		# same test as above but with seek to 'Aug 14 11:59:04' - so other output ...
		output = ('203.162.223.135', 2, 1124013600.0)
		self.filter.setMaxRetry(2)

		self.filter.addLogPath(GetFailures.FILENAME_03, autoSeek=output[2])
		self.filter.addFailRegex(r"error,relay=<HOST>,.*550 User unknown")
		self.filter.getFailures(GetFailures.FILENAME_03)
		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailures04(self):
		# because of not exact time in testcase04.log (no year), we should always use our test time:
		self.assertEqual(MyTime.time(), 1124013600)
		# should find exact 4 failures for *.186 and 2 failures for *.185, but maxretry is 2, so 3 tickets:
		output = (
				('212.41.96.186', 2, 1124013480.0),
				('212.41.96.186', 2, 1124013600.0),
				('212.41.96.185', 2, 1124013598.0)
		)
		# speedup search using exact date pattern:
		self.filter.setDatePattern((r'^%ExY(?P<_sep>[-/.])%m(?P=_sep)%d[T ]%H:%M:%S(?:[.,]%f)?(?:\s*%z)?',
			r'^(?:%a )?%b %d %H:%M:%S(?:\.%f)?(?: %ExY)?',
			r'^EPOCH'
		))
		self.filter.setMaxRetry(2)
		self.filter.addLogPath(GetFailures.FILENAME_04, autoSeek=0)
		self.filter.addFailRegex(r"Invalid user .* <HOST>")
		self.filter.getFailures(GetFailures.FILENAME_04)

		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailuresWrongChar(self):
		self.filter.checkFindTime = False
		# write wrong utf-8 char:
		fname = tempfile.mktemp(prefix='tmp_fail2ban', suffix='crlf')
		fout = fopen(fname, 'wb')
		try:
			# write:
			for l in (
				b'2015-01-14 20:00:58 user \"test\xf1ing\" from \"192.0.2.0\"\n',          # wrong utf-8 char
				b'2015-01-14 20:00:59 user \"\xd1\xe2\xe5\xf2\xe0\" from \"192.0.2.0\"\n', # wrong utf-8 chars
				b'2015-01-14 20:01:00 user \"testing\" from \"192.0.2.0\"\n'               # correct utf-8 chars
			):
				fout.write(l)
			fout.close()
			#
			output = ('192.0.2.0', 3, 1421262060.0)
			failregex = r"^\s*user \"[^\"]*\" from \"<HOST>\"\s*$"

			# test encoding auto or direct set of encoding:
			for enc in (None, 'utf-8', 'ascii'):
				if enc is not None:
					self.tearDown();self.setUp();
					if DefLogSys.getEffectiveLevel() > 7: DefLogSys.setLevel(7); # ensure decode_line logs always
					self.filter.checkFindTime = False;
					self.filter.setLogEncoding(enc);
				# speedup search using exact date pattern:
				self.filter.setDatePattern(r'^%ExY-%Exm-%Exd %ExH:%ExM:%ExS')
				self.assertNotLogged('Error decoding line');
				self.filter.addLogPath(fname)
				self.filter.addFailRegex(failregex)
				self.filter.getFailures(fname)
				_assert_correct_last_attempt(self, self.filter, output)
				
				self.assertLogged('Error decoding line');
				self.assertLogged('Continuing to process line ignoring invalid characters:', '2015-01-14 20:00:58 user ');
				self.assertLogged('Continuing to process line ignoring invalid characters:', '2015-01-14 20:00:59 user ');

		finally:
			_killfile(fout, fname)

	def testGetFailuresUseDNS(self):
		#unittest.F2B.SkipIfNoNetwork() ## without network it is simulated via cache in utils.
		# We should still catch failures with usedns = no ;-)
		output_yes = (
			('51.159.55.100', 1, 1124013299.0,
			  ['Aug 14 11:54:59 i60p295 sshd[12365]: Failed publickey for roehl from fail2ban.org port 51332 ssh2']
			),
			('51.159.55.100', 1, 1124013539.0,
			  ['Aug 14 11:58:59 i60p295 sshd[12365]: Failed publickey for roehl from ::ffff:51.159.55.100 port 51332 ssh2']
			),
			('2001:bc8:1200:6:208:a2ff:fe0c:61f8', 1, 1124013299.0,
			  ['Aug 14 11:54:59 i60p295 sshd[12365]: Failed publickey for roehl from fail2ban.org port 51332 ssh2']
			),
		)
		if not unittest.F2B.no_network and not DNSUtils.IPv6IsAllowed():
			output_yes = output_yes[0:2]

		output_no = (
			('51.159.55.100', 1, 1124013539.0,
			  ['Aug 14 11:58:59 i60p295 sshd[12365]: Failed publickey for roehl from ::ffff:51.159.55.100 port 51332 ssh2']
			)
		)

		# Actually no exception would be raised -- it will be just set to 'no'
		#self.assertRaises(ValueError,
		#				  FileFilter, None, useDns='wrong_value_for_useDns')

		for useDns, output in (
			('yes',  output_yes),
			('no',   output_no),
			('warn', output_yes)
		):
			self.pruneLog("[test-phase useDns=%s]" % useDns)
			jail = DummyJail()
			filter_ = FileFilter(jail, useDns=useDns)
			filter_.active = True
			filter_.failManager.setMaxRetry(1)	# we might have just few failures

			filter_.addLogPath(GetFailures.FILENAME_USEDNS, autoSeek=False)
			filter_.addFailRegex(r"Failed .* from <HOST>")
			filter_.getFailures(GetFailures.FILENAME_USEDNS)
			_assert_correct_last_attempt(self, filter_, output)

	def testGetFailuresMultiRegex(self):
		output = [
			('141.3.81.106', 8, 1124013541.0)
		]

		self.filter.setMaxRetry(8)
		self.filter.addLogPath(GetFailures.FILENAME_02, autoSeek=False)
		self.filter.addFailRegex(r"Failed .* from <HOST>")
		self.filter.addFailRegex(r"Accepted .* from <HOST>")
		self.filter.getFailures(GetFailures.FILENAME_02)
		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailuresIgnoreRegex(self):
		self.filter.addLogPath(GetFailures.FILENAME_02, autoSeek=False)
		self.filter.addFailRegex(r"Failed .* from <HOST>")
		self.filter.addFailRegex(r"Accepted .* from <HOST>")
		self.filter.addIgnoreRegex("for roehl")

		self.filter.getFailures(GetFailures.FILENAME_02)

		self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)

	def testGetFailuresMultiLine(self):
		output = [
			("192.0.43.10", 1, 1124013598.0),
			("192.0.43.10", 1, 1124013599.0),
			("192.0.43.11", 1, 1124013598.0)
		]
		self.filter.addLogPath(GetFailures.FILENAME_MULTILINE, autoSeek=False)
		self.filter.setMaxLines(100)
		self.filter.addFailRegex(r"^.*rsyncd\[(?P<pid>\d+)\]: connect from .+ \(<HOST>\)$<SKIPLINES>^.+ rsyncd\[(?P=pid)\]: rsync error: .*$")
		self.filter.setMaxRetry(1)

		self.filter.getFailures(GetFailures.FILENAME_MULTILINE)
		
		_assert_correct_last_attempt(self, self.filter, output)

	def testGetFailuresMultiLineIgnoreRegex(self):
		output = [
			("192.0.43.10", 1, 1124013598.0),
			("192.0.43.10", 1, 1124013599.0)
		]
		self.filter.addLogPath(GetFailures.FILENAME_MULTILINE, autoSeek=False)
		self.filter.setMaxLines(100)
		self.filter.addFailRegex(r"^.*rsyncd\[(?P<pid>\d+)\]: connect from .+ \(<HOST>\)$<SKIPLINES>^.+ rsyncd\[(?P=pid)\]: rsync error: .*$")
		self.filter.addIgnoreRegex("rsync error: Received SIGINT")
		self.filter.setMaxRetry(1)

		self.filter.getFailures(GetFailures.FILENAME_MULTILINE)

		_assert_correct_last_attempt(self, self.filter, output)

		self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)

	def testGetFailuresMultiLineMultiRegex(self):
		output = [
			("192.0.43.10", 1, 1124013598.0),
			("192.0.43.10", 1, 1124013599.0),
			("192.0.43.11", 1, 1124013598.0),
			("192.0.43.15", 1, 1124013598.0)
		]
		self.filter.addLogPath(GetFailures.FILENAME_MULTILINE, autoSeek=False)
		self.filter.setMaxLines(100)
		self.filter.addFailRegex(r"^.*rsyncd\[(?P<pid>\d+)\]: connect from .+ \(<HOST>\)$<SKIPLINES>^.+ rsyncd\[(?P=pid)\]: rsync error: .*$")
		self.filter.addFailRegex(r"^.* sendmail\[.*, msgid=<(?P<msgid>[^>]+).*relay=\[<HOST>\].*$<SKIPLINES>^.+ spamd: result: Y \d+ .*,mid=<(?P=msgid)>(,bayes=[.\d]+)?(,autolearn=\S+)?\s*$")
		self.filter.setMaxRetry(1)

		self.filter.getFailures(GetFailures.FILENAME_MULTILINE)

		_assert_correct_last_attempt(self, self.filter, output)

		self.assertRaises(FailManagerEmpty, self.filter.failManager.toBan)


class DNSUtilsTests(unittest.TestCase):

	def testCache(self):
		c = Utils.Cache(maxCount=5, maxTime=60)
		# not available :
		self.assertTrue(c.get('a') is None)
		self.assertEqual(c.get('a', 'test'), 'test')
		# exact 5 elements :
		for i in range(5):
			c.set(i, i)
		for i in range(5):
			self.assertEqual(c.get(i), i)
		# remove unavailable key:
		c.unset('a'); c.unset('a')

	def testCacheMaxSize(self):
		c = Utils.Cache(maxCount=5, maxTime=60)
		# exact 5 elements :
		for i in range(5):
			c.set(i, i)
		self.assertEqual([c.get(i) for i in range(5)], [i for i in range(5)])
		self.assertNotIn(-1, (c.get(i, -1) for i in range(5)))
		# add one - too many:
		c.set(10, i)
		# one element should be removed :
		self.assertIn(-1, (c.get(i, -1) for i in range(5)))
		# test max size (not expired):
		for i in range(10):
			c.set(i, 1)
		self.assertEqual(len(c), 5)

	def testCacheMaxTime(self):
		# test max time (expired, timeout reached) :
		c = Utils.Cache(maxCount=5, maxTime=0.0005)
		for i in range(10):
			c.set(i, 1)
		st = time.time()
		self.assertTrue(Utils.wait_for(lambda: time.time() >= st + 0.0005, 1))
		# we have still 5 elements (or fewer if too slow test machine):
		self.assertTrue(len(c) <= 5)
		# but all that are expiered also:
		for i in range(10):
			self.assertTrue(c.get(i) is None)
		# here the whole cache should be empty:
		self.assertEqual(len(c), 0)
		
	def testOverflowedIPCache(self):
		# test overflow of IP-cache multi-threaded (2 "parasite" threads flooding cache):
		from threading import Thread
		from random import shuffle
		# save original cache and use smaller cache during the test here:
		_org_cache = IPAddr.CACHE_OBJ
		cache = IPAddr.CACHE_OBJ = Utils.Cache(maxCount=5, maxTime=60)
		result = list()
		count = 1 if unittest.F2B.fast else 50
		try:
			# tester procedure of worker:
			def _TestCacheStr2IP(forw=True, result=[], random=False):
				try:
					c = count
					while c:
						c -= 1
						s = range(0, 256, 1) if forw else range(255, -1, -1)
						if random: shuffle([i for i in s])
						for i in s:
							IPAddr('192.0.2.'+str(i), IPAddr.FAM_IPv4)
							IPAddr('2001:db8::'+str(i), IPAddr.FAM_IPv6)
					result.append(None)
				except Exception as e:
					DefLogSys.debug(e, exc_info=True)
					result.append(e)

			# 2 workers flooding it forwards and backwards:
			th1 = Thread(target=_TestCacheStr2IP, args=(True,  result)); th1.start()
			th2 = Thread(target=_TestCacheStr2IP, args=(False, result)); th2.start()
			# and here we flooding it with random IPs too:
			_TestCacheStr2IP(True, result, True)
		finally:
			# wait for end of threads and restore cache:
			th1.join()
			th2.join()
			IPAddr.CACHE_OBJ = _org_cache
		self.assertEqual(result, [None]*3) # no errors
		self.assertTrue(len(cache) <= cache.maxCount)


class DNSUtilsNetworkTests(unittest.TestCase):

	def setUp(self):
		"""Call before every test case."""
		super(DNSUtilsNetworkTests, self).setUp()
		#unittest.F2B.SkipIfNoNetwork()

	## fail2ban.org IPs considering IPv6 support (without network it is simulated via cache in utils).
	EXAMPLE_ADDRS = (
		['51.159.55.100', '2001:bc8:1200:6:208:a2ff:fe0c:61f8'] if unittest.F2B.no_network or DNSUtils.IPv6IsAllowed() else \
		['51.159.55.100']
	)

	def test_IPAddr(self):
		ip4 = IPAddr('192.0.2.1')
		ip6 = IPAddr('2001:DB8::')
		self.assertTrue(ip4.isIPv4)
		self.assertTrue(ip4.isSingle)
		self.assertTrue(ip6.isIPv6)
		self.assertTrue(ip6.isSingle)
		self.assertTrue(asip('192.0.2.1').isIPv4)
		self.assertTrue(id(asip(ip4)) == id(ip4))
		# ::
		ip6 = IPAddr('::')
		self.assertTrue(ip6.isIPv6)
		self.assertTrue(ip6.isSingle)
		# ::/32
		ip6 = IPAddr('::/32')
		self.assertTrue(ip6.isIPv6)
		self.assertFalse(ip6.isSingle)
		# path as ID, conversion as unspecified, fallback to raw (cover confusion with the /CIDR):
		for s in ('some/path/as/id', 'other-path/24', '1.2.3.4/path'):
			r = IPAddr(s, IPAddr.CIDR_UNSPEC)
			self.assertEqual(r.raw, s)
			self.assertFalse(r.isIPv4)
			self.assertFalse(r.isIPv6)

	def test_IPAddr_Raw(self):
		# raw string:
		r = IPAddr('xxx', IPAddr.CIDR_RAW)
		self.assertFalse(r.isIPv4)
		self.assertFalse(r.isIPv6)
		self.assertFalse(r.isSingle)
		self.assertTrue(r.isValid)
		self.assertEqual(r, 'xxx')
		self.assertEqual('xxx', str(r))
		self.assertNotEqual(r, IPAddr('xxx'))
		# raw (not IP, for example host:port as string):
		r = IPAddr('1:2', IPAddr.CIDR_RAW)
		self.assertFalse(r.isIPv4)
		self.assertFalse(r.isIPv6)
		self.assertFalse(r.isSingle)
		self.assertTrue(r.isValid)
		self.assertEqual(r, '1:2')
		self.assertEqual('1:2', str(r))
		self.assertNotEqual(r, IPAddr('1:2'))
		# raw vs ip4 (raw is not an ip):
		r = IPAddr('93.184.0.1', IPAddr.CIDR_RAW)
		ip4 = IPAddr('93.184.0.1')
		self.assertNotEqual(ip4, r)
		self.assertNotEqual(r, ip4)
		self.assertTrue(r < ip4)
		self.assertTrue(r < ip4)
		# raw vs ip6 (raw is not an ip):
		r = IPAddr('1::2', IPAddr.CIDR_RAW)
		ip6 = IPAddr('1::2')
		self.assertNotEqual(ip6, r)
		self.assertNotEqual(r, ip6)
		self.assertTrue(r < ip6)
		self.assertTrue(r < ip6)

	def testUseDns(self):
		res = DNSUtils.textToIp('www.fail2ban.org', 'no')
		self.assertSortedEqual(res, [])
		#unittest.F2B.SkipIfNoNetwork() ## without network it is simulated via cache in utils.
		res = DNSUtils.textToIp('www.fail2ban.org', 'warn')
		# sort ipaddr, IPv4 is always smaller as IPv6
		self.assertSortedEqual(res, self.EXAMPLE_ADDRS)
		res = DNSUtils.textToIp('www.fail2ban.org', 'yes')
		# sort ipaddr, IPv4 is always smaller as IPv6
		self.assertSortedEqual(res, self.EXAMPLE_ADDRS)

	def testTextToIp(self):
		#unittest.F2B.SkipIfNoNetwork() ## without network it is simulated via cache in utils.
		# Test hostnames
		hostnames = [
			'www.fail2ban.org',
			'doh1.2.3.4.buga.xxxxx.yyy.invalid',
			'1.2.3.4.buga.xxxxx.yyy.invalid',
			]
		for s in hostnames:
			res = DNSUtils.textToIp(s, 'yes')
			if s == 'www.fail2ban.org':
				# sort ipaddr, IPv4 is always smaller as IPv6
				self.assertSortedEqual(res, self.EXAMPLE_ADDRS)
			else:
				self.assertSortedEqual(res, [])

	def testIpToIp(self):
		# pure ips:
		for s in self.EXAMPLE_ADDRS:
			#if DNSUtils.IPv6IsAllowed(): continue
			ips = DNSUtils.textToIp(s, 'yes')
			self.assertSortedEqual(ips, [s])
			for ip in ips:
				self.assertTrue(isinstance(ip, IPAddr))

	def testIpToName(self):
		#unittest.F2B.SkipIfNoNetwork()
		self.assertEqual(DNSUtils.ipToName('87.142.124.10'), 'test-host')
		self.assertEqual(DNSUtils.ipToName('2001:db8::ffff'), 'test-other')
		res = DNSUtils.ipToName('199.9.14.201')
		self.assertTrue(res.endswith(('.isi.edu', '.b.root-servers.org')))
		# same as above, but with IPAddr:
		res = DNSUtils.ipToName(IPAddr('199.9.14.201'))
		self.assertTrue(res.endswith(('.isi.edu', '.b.root-servers.org')))
		# invalid ip (TEST-NET-1 according to RFC 5737)
		res = DNSUtils.ipToName('192.0.2.0')
		self.assertEqual(res, None)
		# invalid ip:
		res = DNSUtils.ipToName('192.0.2.888')
		self.assertEqual(res, None)

	def testAddr2bin(self):
		res = IPAddr('10.0.0.0')
		self.assertEqual(res.addr, 167772160)
		res = IPAddr('10.0.0.0', cidr=None)
		self.assertEqual(res.addr, 167772160)
		res = IPAddr('10.0.0.0', cidr=32)
		self.assertEqual(res.addr, 167772160)
		res = IPAddr('10.0.0.1', cidr=32)
		self.assertEqual(res.addr, 167772161)
		self.assertTrue(res.isSingle)
		res = IPAddr('10.0.0.1', cidr=31)
		self.assertEqual(res.addr, 167772160)
		self.assertFalse(res.isSingle)

		self.assertEqual(IPAddr('10.0.0.0').hexdump, '0a000000')
		self.assertEqual(IPAddr('1::2').hexdump, '00010000000000000000000000000002')
		self.assertEqual(IPAddr('xxx').hexdump, '')

		self.assertEqual(IPAddr('192.0.2.0').getPTR(), '0.2.0.192.in-addr.arpa.')
		self.assertEqual(IPAddr('192.0.2.1').getPTR(), '1.2.0.192.in-addr.arpa.')
		self.assertEqual(IPAddr('2001:db8::1').getPTR(), 
			'1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa.')

	def testIPAddr_Equal6(self):
		self.assertEqual(
			IPAddr('2606:2800:220:1:248:1893::'),
			IPAddr('2606:2800:220:1:248:1893:0:0')
		)
		# special case IPv6 in brackets:
		self.assertEqual(
			IPAddr('[2606:2800:220:1:248:1893::]'),
			IPAddr('2606:2800:220:1:248:1893:0:0')
		)

	def testIPAddr_InInet(self):
		ip4net = IPAddr('93.184.0.1/24')
		ip6net = IPAddr('2606:2800:220:1:248:1893:25c8:0/120')
		self.assertFalse(ip4net.isSingle)
		self.assertFalse(ip6net.isSingle)
		# ip4:
		self.assertTrue(IPAddr('93.184.0.1').isInNet(ip4net))
		self.assertTrue(IPAddr('93.184.0.255').isInNet(ip4net))
		self.assertFalse(IPAddr('93.184.1.0').isInNet(ip4net))
		self.assertFalse(IPAddr('93.184.0.1').isInNet(ip6net))
		# ip6:
		self.assertTrue(IPAddr('2606:2800:220:1:248:1893:25c8:1').isInNet(ip6net))
		self.assertTrue(IPAddr('2606:2800:220:1:248:1893:25c8:ff').isInNet(ip6net))
		self.assertFalse(IPAddr('2606:2800:220:1:248:1893:25c8:100').isInNet(ip6net))
		self.assertFalse(IPAddr('2606:2800:220:1:248:1893:25c8:100').isInNet(ip4net))
		# raw not in net:
		self.assertFalse(IPAddr('93.184.0.1', IPAddr.CIDR_RAW).isInNet(ip4net))
		self.assertFalse(IPAddr('2606:2800:220:1:248:1893:25c8:1', IPAddr.CIDR_RAW).isInNet(ip6net))
		# invalid not in net:
		self.assertFalse(IPAddr('xxx').isInNet(ip4net))
		# different forms in ::/32:
		ip6net = IPAddr('::/32')
		self.assertTrue(IPAddr('::').isInNet(ip6net))
		self.assertTrue(IPAddr('::1').isInNet(ip6net))
		self.assertTrue(IPAddr('0000::').isInNet(ip6net))
		self.assertTrue(IPAddr('0000::0000').isInNet(ip6net))
		self.assertTrue(IPAddr('0000:0000:7777::').isInNet(ip6net))
		self.assertTrue(IPAddr('0000::7777:7777:7777:7777:7777:7777').isInNet(ip6net))
		self.assertTrue(IPAddr('0000:0000:ffff::').isInNet(ip6net))
		self.assertTrue(IPAddr('0000::ffff:ffff:ffff:ffff:ffff:ffff').isInNet(ip6net))
		self.assertFalse(IPAddr('0000:0001:ffff::').isInNet(ip6net))
		self.assertFalse(IPAddr('1::').isInNet(ip6net))

	def testIPAddr_Compare(self):
		ip4 = [
			IPAddr('192.0.0.1'),
			IPAddr('192.0.2.1'),
			IPAddr('192.0.2.14')
		]
		ip6 = [
			IPAddr('2001:db8::'),
			IPAddr('2001:db8::80da:af6b:0'),
			IPAddr('2001:db8::80da:af6b:8b2c')
		]
		# ip4
		self.assertNotEqual(ip4[0], None)
		self.assertTrue(ip4[0] is not None)
		self.assertFalse(ip4[0] is None)
		self.assertTrue(ip4[0] < ip4[1])
		self.assertTrue(ip4[1] < ip4[2])
		self.assertEqual(sorted(reversed(ip4)), ip4)
		# ip6
		self.assertNotEqual(ip6[0], None)
		self.assertTrue(ip6[0] is not None)
		self.assertFalse(ip6[0] is None)
		self.assertTrue(ip6[0] < ip6[1])
		self.assertTrue(ip6[1] < ip6[2])
		self.assertEqual(sorted(reversed(ip6)), ip6)
		# ip4 vs ip6
		self.assertNotEqual(ip4[0], ip6[0])
		self.assertTrue(ip4[0] < ip6[0])
		self.assertTrue(ip4[2] < ip6[2])
		self.assertEqual(sorted(reversed(ip4+ip6)), ip4+ip6)
		# hashing (with string as key):
		d={
			'192.0.2.14': 'ip4-test', 
			'2001:db8::80da:af6b:8b2c': 'ip6-test'
		}
		d2 = dict([(IPAddr(k), v) for k, v in d.items()])
		self.assertTrue(isinstance(list(d.keys())[0], str))
		self.assertTrue(isinstance(list(d2.keys())[0], IPAddr))
		self.assertEqual(d.get(ip4[2], ''), 'ip4-test')
		self.assertEqual(d.get(ip6[2], ''), 'ip6-test')
		self.assertEqual(d2.get(str(ip4[2]), ''), 'ip4-test')
		self.assertEqual(d2.get(str(ip6[2]), ''), 'ip6-test')
		# compare with string direct:
		self.assertEqual(d, d2)

	def testIPAddr_CIDR(self):
		self.assertEqual(str(IPAddr('93.184.0.1', 24)), '93.184.0.0/24')
		self.assertEqual(str(IPAddr('192.168.1.0/255.255.255.128')), '192.168.1.0/25')
		self.assertEqual(IPAddr('93.184.0.1', 24).ntoa, '93.184.0.0/24')
		self.assertEqual(IPAddr('192.168.1.0/255.255.255.128').ntoa, '192.168.1.0/25')

		self.assertEqual(IPAddr('93.184.0.1/32').ntoa, '93.184.0.1')
		self.assertEqual(IPAddr('93.184.0.1/255.255.255.255').ntoa, '93.184.0.1')

		self.assertEqual(str(IPAddr('2606:2800:220:1:248:1893:25c8::', 120)), '2606:2800:220:1:248:1893:25c8:0/120')
		self.assertEqual(IPAddr('2606:2800:220:1:248:1893:25c8::', 120).ntoa, '2606:2800:220:1:248:1893:25c8:0/120')
		self.assertEqual(str(IPAddr('2606:2800:220:1:248:1893:25c8:0/120')), '2606:2800:220:1:248:1893:25c8:0/120')
		self.assertEqual(IPAddr('2606:2800:220:1:248:1893:25c8:0/120').ntoa, '2606:2800:220:1:248:1893:25c8:0/120')

		self.assertEqual(str(IPAddr('2606:28ff:220:1:248:1893:25c8::', 25)), '2606:2880::/25')
		self.assertEqual(str(IPAddr('2606:28ff:220:1:248:1893:25c8::/ffff:ff80::')), '2606:2880::/25')
		self.assertEqual(str(IPAddr('2606:28ff:220:1:248:1893:25c8::/ffff:ffff:ffff:ffff:ffff:ffff:ffff::')), 
			'2606:28ff:220:1:248:1893:25c8:0/112')

		self.assertEqual(str(IPAddr('2606:28ff:220:1:248:1893:25c8::/128')), 
			'2606:28ff:220:1:248:1893:25c8:0')
		self.assertEqual(str(IPAddr('2606:28ff:220:1:248:1893:25c8::/ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff')), 
			'2606:28ff:220:1:248:1893:25c8:0')

	def testIPAddr_CIDR_Wrong(self):
		# too many plen representations:
		s = '2606:28ff:220:1:248:1893:25c8::/ffff::/::1'
		r = IPAddr(s)
		self.assertEqual(r.raw, s)
		self.assertFalse(r.isIPv4)
		self.assertFalse(r.isIPv6)

	def testIPAddr_CIDR_Repr(self):
		self.assertEqual(["127.0.0.0/8", "::/32", "2001:db8::/32"],
			[IPAddr("127.0.0.0", 8), IPAddr("::1", 32), IPAddr("2001:db8::", 32)]
		)

	def testIPAddr_CompareDNS(self):
		#unittest.F2B.SkipIfNoNetwork() ## without network it is simulated via cache in utils.
		ips = IPAddr('fail2ban.org')
		self.assertTrue(IPAddr("51.159.55.100").isInNet(ips))
		self.assertEqual(IPAddr("2001:bc8:1200:6:208:a2ff:fe0c:61f8").isInNet(ips),
		                        "2001:bc8:1200:6:208:a2ff:fe0c:61f8" in self.EXAMPLE_ADDRS)

	def testIPAddr_wrongDNS_IP(self):
		unittest.F2B.SkipIfNoNetwork()
		DNSUtils.dnsToIp('`this`.dns-is-wrong.`wrong-nic`-dummy')
		DNSUtils.ipToName('*')

	def testIPAddr_Cached(self):
		ips = [DNSUtils.dnsToIp('fail2ban.org'), DNSUtils.dnsToIp('fail2ban.org')]
		for ip1, ip2 in zip(ips, ips):
			self.assertEqual(id(ip1), id(ip2))
		ip1 = IPAddr('51.159.55.100'); ip2 = IPAddr('51.159.55.100'); self.assertEqual(id(ip1), id(ip2))
		ip1 = IPAddr('2001:bc8:1200:6:208:a2ff:fe0c:61f8'); ip2 = IPAddr('2001:bc8:1200:6:208:a2ff:fe0c:61f8'); self.assertEqual(id(ip1), id(ip2))

	def test_NetworkInterfacesAddrs(self):
		for withMask in (False, True):
			try:
				ips = IPAddrSet([a for ni, a in DNSUtils._NetworkInterfacesAddrs(withMask)])
				ip = IPAddr('127.0.0.1')
				self.assertEqual(ip in ips, any(ip in n for n in ips))
				ip = IPAddr('::1')
				self.assertEqual(ip in ips, any(ip in n for n in ips))
			except Exception as e: # pragma: no cover
				# simply skip if not available, TODO: make coverage platform dependent
				raise unittest.SkipTest(e)

	def test_IPAddrSet(self):
		ips = IPAddrSet([IPAddr('192.0.2.1/27'), IPAddr('2001:DB8::/32')])
		self.assertTrue(IPAddr('192.0.2.1') in ips)
		self.assertTrue(IPAddr('192.0.2.31') in ips)
		self.assertFalse(IPAddr('192.0.2.32') in ips)
		self.assertTrue(IPAddr('2001:DB8::1') in ips)
		self.assertTrue(IPAddr('2001:0DB8:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF') in ips)
		self.assertFalse(IPAddr('2001:DB9::') in ips)
		# self IPs must be a set too (cover different mechanisms to obtain own IPs):
		for cov in ('ni', 'dns', 'last'):
			_org_NetworkInterfacesAddrs = None
			if cov == 'dns': # mock-up _NetworkInterfacesAddrs like it's not implemented (raises error)
				_org_NetworkInterfacesAddrs = DNSUtils._NetworkInterfacesAddrs
				def _tmp_NetworkInterfacesAddrs():
					raise NotImplementedError()
				DNSUtils._NetworkInterfacesAddrs = staticmethod(_tmp_NetworkInterfacesAddrs)
			try:
				ips = DNSUtils.getSelfIPs()
				# print('*****', ips)
				if ips:
					ip = IPAddr('127.0.0.1')
					self.assertEqual(ip in ips, any(ip in n for n in ips))
					ip = IPAddr('127.0.0.2')
					self.assertEqual(ip in ips, any(ip in n for n in ips))
					ip = IPAddr('::1')
					self.assertEqual(ip in ips, any(ip in n for n in ips))
			finally:
				if _org_NetworkInterfacesAddrs:
					DNSUtils._NetworkInterfacesAddrs = staticmethod(_org_NetworkInterfacesAddrs)
				if cov != 'last':
					DNSUtils.CACHE_nameToIp.unset(DNSUtils._getSelfIPs_key)
					DNSUtils.CACHE_nameToIp.unset(DNSUtils._getNetIntrfIPs_key)

	def test_FileIPAddrSet(self):
		fname = os.path.join(TEST_FILES_DIR, "test-ign-ips-file")
		ips = DNSUtils.getIPsFromFile(fname)
		for ip, v in IgnoreIP.TEST_IPS_IGN_FILE.items():
			self.assertEqual(IPAddr(ip) in ips, v, ("for %r in test-ign-ips-file\n containing %s)" % (ip, set(ips))))

	def test_FileIPAddrSet_Update(self):
		fname = tempfile.mktemp(prefix='tmp_fail2ban', suffix='.ips')
		f = open(fname, 'wb')
		try:
			f.write(b"192.0.2.200, 192.0.2.201\n")
			f.flush()
			ips = DNSUtils.getIPsFromFile(fname)
			self.assertTrue(IPAddr('192.0.2.200') in ips)
			self.assertTrue(IPAddr('192.0.2.201') in ips)
			self.assertFalse(IPAddr('192.0.2.202') in ips)
			# +1m, jump to next minute to force next check for update:
			MyTime.setTime(MyTime.time() + 60)
			# add .202, some comment and check all 3 IPs are there:
			f.write(b"""192.0.2.202\n
			  # 2001:db8::ca/127         ; IPv6 commented yet
			""")
			f.flush()
			self.assertTrue(IPAddr('192.0.2.200') in ips)
			self.assertTrue(IPAddr('192.0.2.201') in ips)
			self.assertTrue(IPAddr('192.0.2.202') in ips)
			self.assertFalse(IPAddr('2001:db8::ca') in ips)
			self.assertFalse(IPAddr('2001:db8::cb') in ips)
			# +1m, jump to next minute to force next check for update:
			MyTime.setTime(MyTime.time() + 60)
			# remove .200, add IPv6-subnet and check all new IPs are there:
			f.seek(0); f.truncate()
			f.write(b"""
				# 192.0.2.200              ; commented
				192.0.2.201, 192.0.2.202   # no .200 anymore
				2001:db8::ca/127           ; but 2 new IPv6
			""")
			f.flush()
			self.assertFalse(IPAddr('192.0.2.200') in ips)
			self.assertTrue(IPAddr('192.0.2.201') in ips)
			self.assertTrue(IPAddr('192.0.2.202') in ips)
			self.assertTrue(IPAddr('2001:db8::ca') in ips)
			self.assertTrue(IPAddr('2001:db8::cb') in ips)
			# +1m, jump to next minute to force next check for update:
			MyTime.setTime(MyTime.time() + 60)
			self.assertFalse(ips._isModified()); # must be unchanged
			self.assertEqual(ips._isModified(), None); # not checked by latency (same time)
			f.write(b"""#END of file\n""")
			f.flush()
			# +1m, jump to next minute to force next check for update:
			MyTime.setTime(MyTime.time() + 60)
			self.assertTrue(ips._isModified()); # must be modified
			self.assertEqual(ips._isModified(), None); # not checked by latency (same time)
		finally:
			tearDownMyTime()
			_killfile(f, fname)

	def testFQDN(self):
		unittest.F2B.SkipIfNoNetwork()
		sname = DNSUtils.getHostname(fqdn=False)
		lname = DNSUtils.getHostname(fqdn=True)
		# FQDN is not localhost if short hostname is not localhost too (or vice versa):
		self.assertEqual(lname != 'localhost',
		                 sname != 'localhost')
		# FQDN from short name should be long name:
		self.assertEqual(getfqdn(sname), lname)
		# FQDN from FQDN is the same:
		self.assertEqual(getfqdn(lname), lname)
		# coverage (targeting all branches): FQDN from loopback and DNS blackhole is always the same:
		self.assertIn(getfqdn('localhost.'), ('localhost', 'localhost.'))
	
	def testFQDN_DNS(self):
		unittest.F2B.SkipIfNoNetwork()
		self.assertIn(getfqdn('as112.arpa.'), ('as112.arpa.', 'as112.arpa'))


class JailTests(unittest.TestCase):

	def testSetBackend_gh83(self):
		# smoke test
		# Must not fail to initiate
		Jail('test', backend='polling')


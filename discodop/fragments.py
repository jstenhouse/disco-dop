"""Extract recurring tree fragments from constituency treebanks.

NB: there is a known bug in multiprocessing which makes it impossible to detect
Ctrl-C or fatal errors like segmentation faults in child processes which causes
the master program to wait forever for output from its children. Therefore if
you want to abort, kill the program manually (e.g., press Ctrl-Z and issue
'kill %1'). If the program seems stuck, re-run without multiprocessing
(pass --numproc 1) to see if there might be a bug."""

from __future__ import division, print_function, absolute_import, \
		unicode_literals
import io
import os
import re
import sys
import codecs
import logging
if sys.version[0] == '2':
	from itertools import imap as map  # pylint: disable=E0611,W0622
import multiprocessing
from collections import defaultdict
from itertools import count
from getopt import gnu_getopt, GetoptError
from discodop.tree import Tree
from discodop.treebank import READERS
from discodop.treetransforms import binarize, introducepreterminals, unbinarize
from discodop import _fragments
from discodop.parser import workerfunc

USAGE = '''\
Usage: %(cmd)s <treebank1> [treebank2] [options]
  or: %(cmd)s --batch=<dir> <treebank1> <treebank2>... [options]
If only one treebank is given, fragments occurring at least twice are sought.
If two treebanks are given, finds common fragments between first & second.
Input is in Penn treebank format (S-expressions), one tree per line.
Output contains lines of the form "tree<TAB>frequency".
Frequencies refer to the first treebank by default.
Output is sent to stdout; to save the results, redirect to a file.
Options:
  --fmt=(%(fmts)s)
                when format is not 'bracket', work with discontinuous trees;
                output is in 'discbracket' format:
                tree<TAB>sentence<TAB>frequency
                where 'tree' has indices as leaves, referring to elements of
                'sentence', a space separated list of words.
  -o file       Write output to 'file' instead of stdout.
  --indices     report sets of 0-based indices instead of frequencies.
  --cover=n[,m] include all non-maximal/non-recurring fragments up to depth n
                of first treebank; optionally, limit number of substitution
				sites to m.
  --complete    find complete matches of fragments from treebank1 (needle) in
                treebank2 (haystack); frequencies are from haystack.
  --batch=dir   enable batch mode; any number of treebanks > 1 can be given;
                first treebank (A) will be compared to all others (B).
                Results are written to filenames of the form dir/A_B.
                Counts/indices are from B.
  --numproc=n   use n independent processes, to enable multi-core usage
                (default: 1); use 0 to detect the number of CPUs.
  --numtrees=n  only read first n trees from first treebank
  --encoding=x  use x as treebank encoding, e.g. UTF-8, ISO-8859-1, etc.
  --nofreq      do not report frequencies.
  --approx      report counts of occurrence as maximal fragment (lower bound)
  --relfreq     report relative frequencies wrt. root node of fragments.
  --debin       debinarize fragments.
  --twoterms    only consider fragments with at least two lexical terminals.
  --adjacent    only consider pairs of adjacent fragments (n, n + 1).
  --alt         alternative output format: (NP (DT "a") NN)
                default: (NP (DT a) (NN ))
  --debug       extra debug information, ignored when numproc > 1.
  --quiet       disable all messages.\
''' % dict(cmd=sys.argv[0], fmts='|'.join(READERS))

FLAGS = ('approx', 'indices', 'nofreq', 'complete', 'complement', 'alt',
'relfreq', 'twoterms', 'adjacent', 'debin', 'debug', 'quiet', 'help')
OPTIONS = ('fmt=', 'numproc=', 'numtrees=', 'encoding=', 'batch=', 'cover=')
PARAMS = {}
FRONTIERRE = re.compile(r"\(([^ ()]+) \)")
TERMRE = re.compile(r"\(([^ ()]+) ([^ ()]+)\)")
APPLY = lambda x, _y: x()


def main(argv=None):
	"""Command line interface to fragment extraction."""
	if argv is None:
		argv = sys.argv
	try:
		opts, args = gnu_getopt(argv[1:], 'ho:', FLAGS + OPTIONS)
	except GetoptError as err:
		print('error:', err, file=sys.stderr)
		print(USAGE)
		sys.exit(2)
	opts = dict(opts)
	if '--help' in opts or '-h' in opts:
		print(USAGE)
		return

	for flag in FLAGS:
		PARAMS[flag] = '--' + flag in opts
	PARAMS['disc'] = opts.get('--fmt', 'bracket') != 'bracket'
	PARAMS['fmt'] = opts.get('--fmt', 'bracket')
	numproc = int(opts.get('--numproc', 1))
	if numproc == 0:
		numproc = cpu_count()
	if not numproc:
		raise ValueError('numproc should be an integer > 0. got: %r' % numproc)
	limit = int(opts.get('--numtrees', 0)) or None
	PARAMS['cover'] = None
	if '--cover' in opts and ',' in opts['--cover']:
		a, b = opts['--cover'].split(',')
		PARAMS['cover'] = int(a), int(b)
	elif '--cover' in opts:
		PARAMS['cover'] = int(opts.get('--cover', 0)), 999
	encoding = opts.get('--encoding', 'utf8')
	batchdir = opts.get('--batch')

	if len(args) < 1:
		print('missing treebank argument')
	if batchdir is None and len(args) not in (1, 2):
		print('incorrect number of arguments:', args, file=sys.stderr)
		print(USAGE)
		sys.exit(2)
	if batchdir:
		if numproc != 1:
			raise ValueError('Batch mode only supported in single-process '
				'mode. Use the xargs command for multi-processing.')
	if args[0] == '-':
		args[0] = '/dev/stdin'
	for a in args:
		if not os.path.exists(a):
			raise ValueError('not found: %r' % a)
	if PARAMS['complete']:
		if len(args) < 2:
			raise ValueError('need at least two treebanks with --complete.')
		if PARAMS['twoterms'] or PARAMS['adjacent']:
			raise ValueError('--twoterms and --adjacent are incompatible '
					'with --complete.')
		if PARAMS['approx'] or PARAMS['nofreq']:
			raise ValueError('--complete is incompatible with --nofreq '
					'and --approx')

	level = logging.WARNING if PARAMS['quiet'] else logging.DEBUG
	logging.basicConfig(level=level, format='%(message)s')
	if PARAMS['debug'] and numproc > 1:
		logger = multiprocessing.log_to_stderr()
		logger.setLevel(multiprocessing.SUBDEBUG)

	logging.info('Disco-DOP Fragment Extractor')

	logging.info('parameters:\n%s', '\n'.join('    %s:\t%r' % kv
		for kv in sorted(PARAMS.items())))
	logging.info('\n'.join('treebank%d: %s' % (n + 1, a)
		for n, a in enumerate(args)))

	if numproc == 1 and batchdir:
		batch(batchdir, args, limit, encoding, '--debin' in opts)
	else:
		fragmentkeys, counts = regular(args, numproc, limit, encoding)
		out = (io.open(opts['-o'], 'w', encoding=encoding)
				if '-o' in opts else None)
		if '--debin' in opts:
			fragmentkeys = debinarize(fragmentkeys)
		printfragments(fragmentkeys, counts, out=out)


def regular(filenames, numproc, limit, encoding):
	"""non-batch processing. multiprocessing optional."""
	mult = 1
	if PARAMS['approx']:
		fragments = defaultdict(int)
	else:
		fragments = {}
	# detect corpus reading errors in this process (e.g., wrong encoding)
	initworker(filenames[0], filenames[1] if len(filenames) == 2 else None,
			limit, encoding)
	if numproc == 1:
		mymap = map
	else:  # multiprocessing, start worker processes
		pool = multiprocessing.Pool(
				processes=numproc, initializer=initworker,
				initargs=(filenames[0], filenames[1] if len(filenames) == 2
					else None, limit, encoding))
		mymap = pool.imap
	numtrees = (PARAMS['trees1'].len if limit is None
			else min(PARAMS['trees1'].len, limit))

	if PARAMS['complete']:
		trees1, trees2 = PARAMS['trees1'], PARAMS['trees2']
		fragments = _fragments.completebitsets(
				trees1, PARAMS['sents1'], PARAMS['labels'],
				max(trees1.maxnodes, trees2.maxnodes), PARAMS['disc'])
	else:
		if len(filenames) == 1:
			work = workload(numtrees, mult, numproc)
		else:
			chunk = numtrees // (mult * numproc) + 1
			work = [(a, a + chunk) for a in range(0, numtrees, chunk)]
		if numproc != 1:
			logging.info('work division:\n%s', '\n'.join('    %s:\t%r' % kv
				for kv in sorted(dict(numchunks=len(work), mult=mult).items())))
		dowork = mymap(worker, work)
		for results in dowork:
			if PARAMS['approx']:
				for frag, x in results.items():
					fragments[frag] += x
			else:
				fragments.update(results)
	fragmentkeys = list(fragments)
	if PARAMS['nofreq']:
		counts = None
	elif PARAMS['approx']:
		counts = [fragments[a] for a in fragmentkeys]
	else:
		task = 'indices' if PARAMS['indices'] else 'counts'
		logging.info('dividing work for exact %s', task)
		bitsets = [fragments[a] for a in fragmentkeys]
		countchunk = len(bitsets) // numproc + 1
		work = list(range(0, len(bitsets), countchunk))
		work = [(n, len(work), bitsets[a:a + countchunk])
				for n, a in enumerate(work)]
		counts = []
		logging.info('getting exact %s', task)
		for a in mymap(exactcountworker, work):
			counts.extend(a)
	if PARAMS['cover']:
		maxdepth, maxfrontier = PARAMS['cover']
		before = len(fragmentkeys)
		cover = _fragments.allfragments(PARAMS['trees1'], PARAMS['sents1'],
				PARAMS['labels'], maxdepth, maxfrontier, PARAMS['disc'],
				PARAMS['indices'])
		for a in cover:
			if a not in fragments:
				fragmentkeys.append(a)
				counts.append(cover[a])
		logging.info('merged %d cover fragments '
				'up to depth %d with max %d frontier non-terminals.',
				len(fragmentkeys) - before, maxdepth, maxfrontier)
	if numproc != 1:
		pool.close()
		pool.join()
		del dowork, pool
	return fragmentkeys, counts


def batch(outputdir, filenames, limit, encoding, debin):
	"""batch processing: three or more treebanks specified.

	Compares the first treebank to all others, and writes the results
	to ``outputdir/A_B`` where ``A`` and ``B`` are the respective filenames.
	Counts/indices are from the other (B) treebanks.
	There are at least 2 use cases for this:

	1. Comparing one treebank to a series of others. The first treebank will
		only be loaded once.
	2. In combination with ``--complete``, the first treebank is a set of
		fragments used as queries on the other treebanks specified."""
	initworker(filenames[0], None, limit, encoding)
	trees1 = PARAMS['trees1']
	sents1 = PARAMS['sents1']
	if PARAMS['complete']:
		fragments = _fragments.completebitsets(
				trees1, sents1, PARAMS['labels'],
				trees1.maxnodes, PARAMS['disc'])
		fragmentkeys = list(fragments)
	elif PARAMS['approx']:
		fragments = defaultdict(int)
	else:
		fragments = {}
	for filename in filenames[1:]:
		PARAMS.update(read2ndtreebank(filename, PARAMS['labels'],
			PARAMS['prods'], PARAMS['fmt'], limit, encoding))
		trees2 = PARAMS['trees2']
		sents2 = PARAMS['sents2']
		if not PARAMS['complete']:
			fragments = _fragments.extractfragments(trees1, sents1, 0, 0,
					PARAMS['labels'], trees2, sents2,
					discontinuous=PARAMS['disc'], debug=PARAMS['debug'],
					approx=PARAMS['approx'],
					twoterms=PARAMS['twoterms'],
					adjacent=PARAMS['adjacent'])
			fragmentkeys = list(fragments)
		counts = None
		if PARAMS['approx'] or not fragments:
			counts = fragments.values()
		elif not PARAMS['nofreq']:
			bitsets = [fragments[a] for a in fragmentkeys]
			logging.info('getting %s for %d fragments',
					'indices of occurrence' if PARAMS['indices']
					else 'exact counts', len(bitsets))
			counts = _fragments.exactcounts(trees1, trees2, bitsets,
					indices=PARAMS['indices'])
		outputfilename = '%s/%s_%s' % (outputdir,
				os.path.basename(filenames[0]), os.path.basename(filename))
		out = io.open(outputfilename, 'w', encoding=encoding)
		if debin:
			fragmentkeys = debinarize(fragmentkeys)
		printfragments(fragmentkeys, counts, out=out)
		logging.info('wrote to %s', outputfilename)


def readtreebanks(treebank1, treebank2=None, fmt='bracket',
		limit=None, encoding='utf-8'):
	"""Read one or two treebanks."""
	labels = []
	prods = {}
	trees1, sents1 = _fragments.readtreebank(treebank1, labels, prods,
			fmt, limit, encoding)
	trees2, sents2 = _fragments.readtreebank(treebank2, labels, prods,
			fmt, limit, encoding)
	trees1.indextrees(prods)
	if trees2:
		trees2.indextrees(prods)
	return dict(trees1=trees1, sents1=sents1, trees2=trees2, sents2=sents2,
			prods=prods, labels=labels)


def read2ndtreebank(treebank2, labels, prods, fmt='bracket',
		limit=None, encoding='utf-8'):
	"""Read a second treebank."""
	trees2, sents2 = _fragments.readtreebank(treebank2, labels, prods,
			fmt, limit, encoding)
	trees2.indextrees(prods)
	logging.info("%r: %d trees; %d nodes (max %d). "
			"labels: %d, prods: %d",
			treebank2, len(trees2), trees2.numnodes, trees2.maxnodes,
			len(set(PARAMS['labels'])), len(PARAMS['prods']))
	return dict(trees2=trees2, sents2=sents2, prods=prods, labels=labels)


def initworker(treebank1, treebank2, limit, encoding):
	"""Read treebanks for this worker.

	We do this separately for each process under the assumption that this is
	advantageous with a NUMA architecture."""
	PARAMS.update(readtreebanks(treebank1, treebank2,
			limit=limit, fmt=PARAMS['fmt'], encoding=encoding))
	if PARAMS['debug']:
		print("\nproductions:")
		for a, b in sorted(PARAMS['prods'].items(), key=lambda x: x[1]):
			print(b, *a)
	trees1 = PARAMS['trees1']
	if not trees1:
		raise ValueError('treebank1 empty.')
	m = "treebank1: %d trees; %d nodes (max: %d);" % (
			trees1.len, trees1.numnodes, trees1.maxnodes)
	if treebank2:
		trees2 = PARAMS['trees2']
		if not trees2:
			raise ValueError('treebank2 empty.')
		m += " treebank2: %d trees; %d nodes (max %d);" % (
				trees2.len, trees2.numnodes, trees2.maxnodes)
	logging.info("%s labels: %d, prods: %d", m, len(set(PARAMS['labels'])),
			len(PARAMS['prods']))


def initworkersimple(trees, sents, disc, trees2=None, sents2=None):
	"""Initialization for a worker in which a treebank was already loaded."""
	PARAMS.update(_fragments.getctrees(trees, sents, disc, trees2, sents2))
	assert PARAMS['trees1']


@workerfunc
def worker(interval):
	"""Worker function for fragment extraction."""
	offset, end = interval
	trees1 = PARAMS['trees1']
	trees2 = PARAMS['trees2']
	sents1 = PARAMS['sents1']
	sents2 = PARAMS['sents2']
	assert offset < trees1.len
	result = {}
	result = _fragments.extractfragments(trees1, sents1, offset, end,
			PARAMS['labels'], trees2, sents2, approx=PARAMS['approx'],
			discontinuous=PARAMS['disc'], complement=PARAMS['complement'],
			debug=PARAMS['debug'], twoterms=PARAMS['twoterms'],
			adjacent=PARAMS['adjacent'])
	logging.debug("finished %d--%d", offset, end)
	return result


@workerfunc
def exactcountworker(args):
	"""Worker function for counting of fragments."""
	n, m, bitsets = args
	trees1 = PARAMS['trees1']
	if PARAMS['complete']:
		results = _fragments.exactcounts(trees1, PARAMS['trees2'], bitsets,
				indices=PARAMS['indices'])
		logging.debug("complete matches %d of %d", n + 1, m)
		return results
	results = _fragments.exactcounts(
			trees1, trees1, bitsets, indices=PARAMS['indices'])
	if PARAMS['indices']:
		logging.debug("exact indices %d of %d", n + 1, m)
	else:
		logging.debug("exact counts %d of %d", n + 1, m)
	return results


def workload(numtrees, mult, numproc):
	"""Calculate an even workload.

	When *n* trees are compared against themselves, ``n * (n - 1)`` total
	comparisons are made. Each tree *m* has to be compared to all trees *x*
	such that ``m < x <= n``
	(meaning there are more comparisons for lower *n*).

	:returns: a sequence of ``(start, end)`` intervals such that
		the number of comparisons is approximately balanced."""
	# could base on number of nodes as well.
	if numproc == 1:
		return [(0, numtrees)]
	# here chunk is the number of tree pairs that will be compared
	goal = togo = total = 0.5 * numtrees * (numtrees - 1)
	chunk = total // (mult * numproc) + 1
	goal -= chunk
	result = []
	last = 0
	for n in range(1, numtrees):
		togo -= numtrees - n
		if togo <= goal:
			goal -= chunk
			result.append((last, n))
			last = n
	if last < numtrees:
		result.append((last, numtrees))
	return result


def recurringfragments(trees, sents, numproc=1, disc=True,
		iterate=False, complement=False, indices=True, maxdepth=1,
		maxfrontier=999):
	"""Get recurring fragments with exact counts in a single treebank.

	:returns: a dictionary whose keys are fragments as strings, and
		indices as values. When ``disc`` is ``True``, keys are of the form
		``(frag, sent)`` where ``frag`` is a unicode string, and ``sent``
		is a list of words as unicode strings; when ``disc`` is ``False``, keys
		are of the form ``frag`` where ``frag`` is a unicode string.
	:param trees: a sequence of binarized Tree objects, with indices as leaves.
	:param sents: the corresponding sentences (lists of strings).
	:param numproc: number of processes to use; pass 0 to use detected # CPUs.
	:param disc: when disc=True, assume trees with discontinuous constituents;
		resulting fragments will be of the form (frag, sent);
		otherwise fragments will be strings with words as leaves.
	:param iterate, complement: see :func:`_fragments.extractfragments`
	:param indices: when False, return integer counts instead of indices.
	:param maxdepth: when > 0, add 'cover' fragments to result, corresponding
		to all fragments up to given depth; pass 0 to disable.
	:param maxfrontier: maximum number of frontier non-terminals (substitution
		sites) in cover fragments; a limit of 0 only gives fragments that
		bottom out in terminals; the default 999 is unlimited for practical
		purposes."""
	if numproc == 0:
		numproc = cpu_count()
	numtrees = len(trees)
	if not numtrees:
		raise ValueError('no trees.')
	mult = 1  # 3 if numproc > 1 else 1
	fragments = {}
	trees = trees[:]
	work = workload(numtrees, mult, numproc)
	PARAMS.update(disc=disc, indices=indices, approx=False, complete=False,
			complement=complement, debug=False, adjacent=False, twoterms=False)
	initworkersimple(trees, list(sents), disc)
	if numproc == 1:
		mymap = map
	else:
		logging.info("work division:\n%s", "\n".join("    %s: %r" % kv
				for kv in sorted(dict(numchunks=len(work),
					numproc=numproc).items())))
		# start worker processes
		pool = multiprocessing.Pool(
				processes=numproc, initializer=initworkersimple,
				initargs=(trees, list(sents), disc))
		mymap = pool.map
	# collect recurring fragments
	logging.info("extracting recurring fragments")
	for a in mymap(worker, work):
		fragments.update(a)
	fragmentkeys = list(fragments)
	bitsets = [fragments[a] for a in fragmentkeys]
	countchunk = len(bitsets) // numproc + 1
	work = list(range(0, len(bitsets), countchunk))
	work = [(n, len(work), bitsets[a:a + countchunk])
			for n, a in enumerate(work)]
	logging.info("getting exact counts for %d fragments", len(bitsets))
	counts = []
	for a in mymap(exactcountworker, work):
		counts.extend(a)
	# add all fragments up to a given depth
	if maxdepth:
		cover = _fragments.allfragments(PARAMS['trees1'], PARAMS['sents1'],
				PARAMS['labels'], maxdepth, maxfrontier, disc, indices)
		before = len(fragmentkeys)
		for a in cover:
			if a not in fragments:
				fragmentkeys.append(a)
				counts.append(cover[a])
		logging.info('merged %d cover fragments '
				'up to depth %d with max %d frontier non-terminals.',
				len(fragmentkeys) - before, maxdepth, maxfrontier)
	if numproc != 1:
		pool.close()
		pool.join()
		del pool
	if iterate:  # optionally collect fragments of fragments
		if not disc:
			raise NotImplementedError
		logging.info("extracting fragments of recurring fragments")
		PARAMS['complement'] = False  # needs to be turned off if it was on
		newfrags = fragments
		trees, sents = None, None
		ids = count()
		for _ in range(10):  # up to 10 iterations
			newtrees = [binarize(
					introducepreterminals(Tree(tree),
					sent, ids=ids), childchar="}") for tree, sent in newfrags]
			newsents = [["#%d" % next(ids) if word is None else word
					for word in sent] for _, sent in newfrags]
			newfrags, newcounts = iteratefragments(
					fragments, newtrees, newsents, trees, sents, numproc)
			if len(newfrags) == 0:
				break
			if trees is None:
				trees = []
				sents = []
			trees.extend(newtrees)
			sents.extend(newsents)
			fragmentkeys.extend(newfrags)
			counts.extend(newcounts)
			fragments.update(zip(newfrags, newcounts))
	logging.info("found %d fragments", len(fragmentkeys))
	return dict(zip(fragmentkeys, counts))


def allfragments(trees, sents, maxdepth, maxfrontier=999):
	"""Return all fragments up to a certain depth, # frontiers."""
	PARAMS.update(disc=True, indices=True, approx=False, complete=False,
			complement=False, debug=False, adjacent=False, twoterms=False)
	initworkersimple(trees, list(sents), PARAMS['disc'])
	return _fragments.allfragments(PARAMS['trees1'], PARAMS['sents1'],
			PARAMS['labels'], maxdepth, maxfrontier,
			discontinuous=PARAMS['disc'], indices=PARAMS['indices'])


def iteratefragments(fragments, newtrees, newsents, trees, sents, numproc):
	"""Get fragments of fragments."""
	numtrees = len(newtrees)
	if not numtrees:
		raise ValueError('no trees.')
	if numproc == 1:  # set fragments as input
		initworkersimple(newtrees, newsents, PARAMS['disc'], trees, sents)
		mymap = map
	else:
		# since the input trees change, we need a new pool each time
		pool = multiprocessing.Pool(
				processes=numproc, initializer=initworkersimple,
				initargs=(newtrees, newsents, PARAMS['disc'], trees, sents))
		mymap = pool.imap
	newfragments = {}
	for a in mymap(worker, workload(numtrees, 1, numproc)):
		newfragments.update(a)
	logging.info("before: %d, after: %d, difference: %d",
		len(fragments), len(set(fragments) | set(newfragments)),
		len(set(newfragments) - set(fragments)))
	# we have to get counts for these separately because they're not coming
	# from the same set of trees
	newkeys = list(set(newfragments) - set(fragments))
	bitsets = [newfragments[a] for a in newkeys]
	countchunk = len(bitsets) // numproc + 1
	if countchunk == 0:
		return newkeys, []
	work = list(range(0, len(bitsets), countchunk))
	work = [(n, len(work), bitsets[a:a + countchunk])
			for n, a in enumerate(work)]
	logging.info("getting exact counts for %d fragments", len(bitsets))
	counts = []
	for a in mymap(exactcountworker, work):
		counts.extend(a)
	if numproc != 1:
		pool.close()
		pool.join()
		del pool
	return newkeys, counts


def altrepr(a):
	"""Rewrite bracketed tree to alternative format.

	Replace double quotes with double single quotes: " -> ''
	Quote terminals with double quotes terminal: -> "terminal"
	Remove parentheses around frontier nodes: (NN ) -> NN

	>>> print(altrepr('(NP (DT a) (NN ))'))
	(NP (DT "a") NN)
	"""
	return FRONTIERRE.sub(r'\1', TERMRE.sub(r'(\1 "\2")', a.replace('"', "''")))


def debinarize(fragments):
	"""Debinarize fragments; fragments that fail to debinarize left as-is."""
	result = []
	for origfrag in fragments:
		if PARAMS['disc']:
			frag, sent = origfrag
		else:
			frag = origfrag
		try:
			frag = '%s' % unbinarize(Tree(frag))
		except Exception:  # pylint: disable=broad-except
			result.append(origfrag)
		else:
			result.append((frag, sent) if PARAMS['disc'] else frag)
	return result


def printfragments(fragments, counts, out=None):
	"""Dump fragments to standard output or some other file object."""
	if out is None:
		out = sys.stdout
		if sys.stdout.encoding is None:
			out = codecs.getwriter('utf8')(out)
	if PARAMS['alt']:
		for n, a in enumerate(fragments):
			fragments[n] = altrepr(a)
	if PARAMS['complete']:
		logging.info('total number of matches: %d',
				sum(sum(a.values()) for a in counts)
				if PARAMS['indices'] else sum(counts))
	else:
		logging.info('number of fragments: %d', len(fragments))
	if PARAMS['nofreq']:
		for a in fragments:
			out.write('%s\n' % (('%s\t%s' % (a[0],
					' '.join('%s' % x if x else '' for x in a[1])))
					if PARAMS['disc'] else a))
		return
	# a frequency of 0 is normal when counting occurrences of given fragments
	# in a second treebank
	if PARAMS['complete']:
		threshold = 0
		zeroinvalid = False
	# a frequency of 1 is normal when comparing two treebanks
	# or when non-recurring fragments are added
	elif (PARAMS.get('trees2') or PARAMS['cover']
			or PARAMS['complement'] or PARAMS['approx']):
		threshold = 0
		zeroinvalid = True
	else:  # otherwise, raise alarm.
		threshold = 1
		zeroinvalid = True
	if PARAMS['indices']:
		for a, theindices in zip(fragments, counts):
			if len(theindices) > threshold:
				out.write('%s\t%r\n' % (('%s\t%s' % (a[0],
					' '.join('%s' % x if x else '' for x in a[1])))
					if PARAMS['disc'] else a,
					[n for n in sorted(theindices.elements())
						if n - 1 in theindices or n + 1 in theindices]
					if PARAMS['adjacent'] else
					list(sorted(theindices.elements()))))
			elif zeroinvalid:
				raise ValueError('invalid fragment--frequency=1: %r' % a)
	elif PARAMS['relfreq']:
		sums = defaultdict(int)
		for a, freq in zip(fragments, counts):
			if freq > threshold:
				sums[a[1:a.index(' ')]] += freq
			elif zeroinvalid:
				raise ValueError('invalid fragment--frequency=%d: %r' % (
					freq, a))
		for a, freq in zip(fragments, counts):
			out.write('%s\t%d/%d\n' % (('%s\t%s' % (a[0],
				' '.join('%s' % x if x else '' for x in a[1])))
				if PARAMS['disc'] else a,
				freq, sums[a[1:a.index(' ')]]))
	else:
		for a, freq in zip(fragments, counts):
			if freq > threshold:
				out.write('%s\t%d\n' % (('%s\t%s' % (a[0],
					' '.join('%s' % x if x else '' for x in a[1])))
					if PARAMS['disc'] else a, freq))
			elif zeroinvalid:
				raise ValueError('invalid fragment--frequency=1: %r' % a)


def cpu_count():
	"""Return number of CPUs or 1."""
	try:
		return multiprocessing.cpu_count()
	except NotImplementedError:
		return 1


def test():
	"""Demonstration of fragment extractor."""
	main('fragments.py --fmt=export alpinosample.export'.split())


__all__ = ['main', 'regular', 'batch', 'readtreebanks', 'read2ndtreebank',
		'initworker', 'initworkersimple', 'worker', 'exactcountworker',
		'workload', 'recurringfragments', 'iteratefragments', 'allfragments',
		'debinarize', 'printfragments', 'altrepr', 'cpu_count']

if __name__ == '__main__':
	main()

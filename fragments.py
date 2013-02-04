""" Fast Fragment Seeker
Extracts recurring fragments from constituency treebanks.

NB: there is a known bug in multiprocessing which makes it impossible to detect
ctrl-c or fatal errors like segmentation faults in children which causes the
master program to wait forever for output from its children. Therefore if
you want to abort, kill the program manually (e.g., press ctrl-z and run
'kill %1'). If the program seems stuck, re-run without multiprocessing
(pass --numproc 1) to see if there might be a bug. """

from __future__ import division, print_function
import io, os, re, sys, logging
from multiprocessing import Pool, cpu_count, log_to_stderr, SUBDEBUG
from collections import defaultdict
from itertools import count
from getopt import gnu_getopt, GetoptError
from treetransforms import binarize, introducepreterminals
from _fragments import extractfragments, fastextractfragments, \
		exactcounts, exactindices, readtreebank, indextrees, getctrees, \
		completebitsets, coverbitsets

# sys.stdout = io.getwriter('utf8')(sys.stdout)
params = {}

USAGE = """Fast Fragment Seeker
usage: %s [options] treebank1 [treebank2]
If only one treebank is given, fragments occurring at least twice are sought.
If two treebanks are given, finds common fragments between first & second.
Input is in Penn treebank format (S-expressions), one tree per line.
Output contains lines of the form "tree<TAB>frequency".
Frequencies always refer to the first treebank.
Output is sent to stdout; to save the results, redirect to a file.
--complete    find complete matches of fragments from treebank2 in treebank1.
--indices     report sets of indices instead of frequencies.
--disc        work with discontinuous trees; input is in Negra export format.
              output: tree<TAB>sentence<TAB>frequency
              where "tree' has indices as leaves, referring to elements of
              "sentence", a space separated list of words.
--cover       include all `cover' fragments corresponding to single productions.
--numproc n   use n independent processes, to enable multi-core usage.
              The default is not to use multiprocessing; use 0 to use all CPUs.
--encoding x  use x as treebank encoding, e.g. UTF-8, ISO-8859-1, etc.
--numtrees n  only read first n trees from first treebank
--batch dir   enable batch mode; any number of treebanks > 1 can be given;
              first treebank will be compared to all others.
              results are written to filenames of the form dir/A_B.
--quadratic   use the slower, quadratic algorithm for finding fragments.
--approx      report approximate frequencies
--nofreq      do not report frequencies.
--alt         alternative output format: (NP (DT "a") NN)
              default: (NP (DT a) (NN ))
--debug       extra debug information, ignored when numproc > 1.
--quiet       disable all messages.""" % sys.argv[0]

FLAGS = ("approx", "indices", "nofreq", "complete", "complement",
		"disc", "quiet", "debug", "quadratic", "cover", "alt")
OPTIONS = ("numproc=", "numtrees=", "encoding=", "batch=")

def main(argv=None):
	""" Command line interface to fragment extraction. """
	if argv is None:
		argv = sys.argv
	try:
		opts, args = gnu_getopt(argv[1:], "", FLAGS + OPTIONS)
	except GetoptError as err:
		print("%s\n%s" % (err, USAGE))
		return
	opts = dict(opts)

	for flag in FLAGS:
		params[flag] = "--" + flag in opts
	numproc = int(opts.get("--numproc", 1))
	if numproc == 0:
		numproc = cpu_count()
	numtrees = int(opts.get("--numtrees", 0))
	encoding = opts.get("--encoding", "UTF-8")
	batch = opts.get("--batch")

	if len(args) < 1:
		print("missing treebank argument")
	if batch is None and len(args) not in (1, 2):
		print("incorrect number of arguments:", args)
		print(USAGE)
		return
	if batch:
		assert numproc == 1, "batch mode only supported in single-process mode"
	if args[0] == "-":
		args[0] = "/dev/stdin"
	for a in args:
		assert os.path.exists(a), "not found: %r" % a
	if params['complete']:
		assert len(args) == 2 or batch, (
		"need at least two treebanks with --complete.")
	level = logging.WARNING if params['quiet'] else logging.INFO
	logging.basicConfig(level=level, format='%(message)s')

	if params['debug'] and numproc > 1:
		logger = log_to_stderr()
		logger.setLevel(SUBDEBUG)

	logging.info("Fast Fragment Seeker")

	assert numproc
	limit = numtrees
	if numtrees == 0:
		if params['disc']:
			numtrees = len([a for a in
					io.open(args[0], encoding=encoding).readlines()
					if a.startswith("#BOS ")])
		else:
			numtrees = len(io.open(args[0], encoding=encoding).readlines())
	assert numtrees
	mult = 1 #3 if numproc > 1 else 1
	if params['approx']:
		fragments = defaultdict(int)
	else:
		fragments = {}
	logging.info("parameters:\n%s", "\n".join("    %s:\t%r" % kv
		for kv in sorted(params.items())))
	logging.info("\n".join("treebank%d: %s" % (n + 1, a)
		for n, a in enumerate(args)))

	if numproc == 1 and batch:
		initworker(args[0], None, limit, encoding)
		trees1 = params['trees1']
		sents1 = params['sents1']
		if params['complete']:
			raise NotImplementedError
		for a in args[1:]:
			params.update(read2ndtreebank(a, params['labels'], params['prods'],
				params['disc'], limit, encoding))
			trees2 = params['trees2']
			sents2 = params['sents2']
			if params['quadratic']:
				fragments = extractfragments(trees1, sents1, 0, 0,
						params['revlabel'], trees2, sents2,
						discontinuous=params['disc'], debug=params['debug'],
						approx=params['approx'])
			else:
				fragments = fastextractfragments(trees1, sents1, 0, 0,
						params['revlabel'], trees2, sents2,
						discontinuous=params['disc'], debug=params['debug'],
						approx=params['approx'])
			if not params['approx'] and not params['nofreq']:
				items = list(fragments.items())
				fragmentkeys = [b for b, _ in items]
				bitsets = [b for _, b in items]
				if params['indices']:
					logging.info("getting indices of occurrence "
							"for %d fragments", len(bitsets))
					counts = exactindices(trees1, trees1, bitsets,
							params['treeswithprod'],
							fast=not params['quadratic'])
				else:
					logging.info("getting exact counts for %d fragments",
							len(bitsets))
					counts = exactcounts(trees1, trees1, bitsets,
							params['treeswithprod'],
							fast=not params['quadratic'])
				fragments = list(zip(fragmentkeys, counts))
			filename = "%s/%s_%s" % (batch, os.path.basename(args[0]),
					os.path.basename(a))
			out = io.open(filename, "w", encoding=encoding)
			printfragments(fragments, out=out)
			logging.info("wrote to %s", filename)
		return

	# multiprocessing part
	if numproc == 1:
		initworker(args[0], args[1] if len(args) == 2 else None,
				limit, encoding)
		mymap = map
		myapply = lambda x, y: x(*y)
	else:
		# detect corpus reading errors in this process (e.g. wrong encoding)
		initworker(args[0], args[1] if len(args) == 2 else None,
				limit, encoding)
		# start worker processes
		pool = Pool(processes=numproc, initializer=
			initworker,
			initargs=(args[0], args[1] if len(args) == 2 else None,
				limit, encoding))
		mymap = pool.imap
		myapply = pool.apply

	if params['complete']:
		initworker(args[0], args[1] if len(args) == 2 else None,
			limit, encoding)
		fragments = completebitsets(params['trees2'],
				sents2 if params['disc'] else None, params['revlabel'])
	else:
		if len(args) == 1:
			work = workload(numtrees, mult, numproc)
		else:
			chunk = numtrees // (mult * numproc) + 1
			work = [(a, a + chunk) for a in range(0, numtrees, chunk)]
		if numproc != 1:
			logging.info("work division:\n%s", "\n".join("    %s:\t%r" % kv
				for kv in sorted(dict(numchunks=len(work), mult=mult).items())))
		dowork = mymap(worker, work)
		for n, a in enumerate(dowork):
			if params['approx']:
				for frag, x in a.items():
					fragments[frag] += x
			else:
				fragments.update(a)
	if params['cover']:
		cover = myapply(coverfragworker, ())
		if params['approx']:
			fragments.update(zip(cover.keys(),
					exactcounts(trees1, trees1, cover.values(),
					params['treeswithprod'], fast=not params['quadratic'])))
		else:
			fragments.update(cover)
		logging.info("merged %d cover fragments", len(cover))
	if params['approx'] or params['nofreq']:
		fragments = list(fragments.items())
	elif fragments:
		task = "indices" if params['indices'] else "counts"
		logging.info("dividing work for exact %s", task)
		items = list(fragments.items())
		fragmentkeys = [a for a, _ in items]
		bitsets = [a for _, a in items]
		countchunk = len(bitsets) // numproc + 1
		work = list(range(0, len(bitsets), countchunk))
		work = [(n, len(work), bitsets[a:a + countchunk])
				for n, a in enumerate(work)]
		logging.info("getting exact %s", task)
		counts = []
		for a in mymap(exactcountworker, work):
			counts.extend(a)
		fragments = list(zip(fragmentkeys, counts))
	if numproc != 1:
		pool.terminate()
		pool.join()
		del dowork, pool
	printfragments(fragments)

def readtreebanks(treebank1, treebank2=None, discontinuous=False,
		limit=0, encoding="utf-8"):
	""" Read one or two treebanks.  """
	labels = {}
	prods = {}
	trees1, sents1 = readtreebank(treebank1, labels, prods,
		not params['quadratic'], discontinuous, limit, encoding)
	trees2, sents2 = readtreebank(treebank2, labels, prods,
		not params['quadratic'], discontinuous, limit, encoding)
	revlabel = sorted(labels, key=labels.get)
	treeswithprod = indextrees(trees1, prods)
	return dict(trees1=trees1, sents1=sents1, trees2=trees2, sents2=sents2,
		labels=labels, prods=prods, revlabel=revlabel,
		treeswithprod=treeswithprod)

def read2ndtreebank(treebank2, labels, prods, discontinuous=False,
	limit=0, encoding="utf-8"):
	""" Read a second treebank.  """
	trees2, sents2 = readtreebank(treebank2, labels, prods,
		not params['quadratic'], discontinuous, limit, encoding)
	revlabel = sorted(labels, key=labels.get)
	return dict(trees2=trees2, sents2=sents2, labels=labels, prods=prods,
		revlabel=revlabel)

def initworker(treebank1, treebank2, limit, encoding):
	""" Read treebanks for this worker. We do this separately for each process
	under the assumption that this is advantageous with a NUMA architecture. """
	params.update(readtreebanks(treebank1, treebank2,
		limit=limit, discontinuous=params['disc'], encoding=encoding))
	if params['debug']:
		print("labels:")
		for a, b in params['labels'].items():
			print(a, b)
		print("\nproductions:")
		for a, b in params['prods'].items():
			print(a, b)
	trees1 = params['trees1']
	assert trees1
	m = "treebank1: %d trees; %d nodes (max: %d);" % (
		len(trees1), trees1.nodes, trees1.maxnodes)
	if treebank2:
		trees2 = params['trees2']
		assert trees2
		m += " treebank2: %d trees; %d nodes (max %d);" % (
			len(trees2), trees2.nodes, trees2.maxnodes)
	logging.info("%s labels: %d, prods: %d", m, len(params['labels']),
		len(params['prods']))

def worker(interval):
	""" Worker function that initiates the extraction of fragments
	in each process. """
	offset, end = interval
	trees1 = params['trees1']
	trees2 = params['trees2']
	sents1 = params['sents1']
	sents2 = params['sents2']
	assert offset < len(trees1)
	result = {}
	if params['quadratic']:
		result = extractfragments(trees1, sents1, offset, end,
				params['revlabel'], trees2, sents2, approx=params['approx'],
				discontinuous=params['disc'], debug=params['debug'])
	else:
		result = fastextractfragments(trees1, sents1, offset, end,
				params['revlabel'], trees2, sents2, approx=params['approx'],
				discontinuous=params['disc'], complement=params['complement'],
				debug=params.get('debug'))
	logging.info("finished %d--%d", offset, end)
	return result

def exactcountworker(args):
	""" Worker function that initiates the counting of fragments
	in each process. """
	n, m, fragments = args
	trees1 = params['trees1']
	if params['indices']:
		results = exactindices(trees1, trees1, fragments,
				params['treeswithprod'], fast=not params['quadratic'])
		logging.info("exact indices %d of %d", n+1, m)
	elif params['complete']:
		results = exactcounts(trees1, params['trees2'], fragments,
				params['treeswithprod'], fast=not params['quadratic'])
		logging.info("complete fragments %d of %d", n+1, m)
	else:
		results = exactcounts(trees1, trees1, fragments,
				params['treeswithprod'], fast=not params['quadratic'])
		logging.info("exact counts %d of %d", n+1, m)
	return results

def coverfragworker():
	""" Worker function that gets depth-1 fragments. Does not need
	multiprocessing but using it avoids reading the treebank again. """
	return coverbitsets(params['trees1'], params['sents1'],
			params['treeswithprod'], params['revlabel'], params['disc'])

def initworkersimple(trees, sents, trees2=None, sents2=None):
	""" A simpler initialization for a worker in which a treebank has already
	been loaded. """
	params.update(getctrees(trees, sents, trees2, sents2))
	assert params['trees1']

def workload(numtrees, mult, numproc):
	""" Get an even workload. When n trees are compared against themselves,
	n * (n - 1) total comparisons are made.
	Each tree m has to be compared to all trees x such that m < x < n.
	(meaning there are more comparisons for lower n).
	This function returns a sequence of (start, end) intervals such that
	the number of comparisons is approximately balanced. """
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

def getfragments(trees, sents, numproc=1, iterate=False, complement=False,
		indices=False):
	""" Get recurring fragments with exact counts in a single treebank. """
	if numproc == 0:
		numproc = cpu_count()
	numtrees = len(trees)
	assert numtrees
	mult = 1 #3 if numproc > 1 else 1
	fragments = {}
	trees = trees[:]
	work = workload(numtrees, mult, numproc)
	params.update(disc=True, indices=indices, approx=False, complete=False,
			quadratic=False, complement=complement)
	if numproc == 1:
		initworkersimple(trees, list(sents))
		mymap = map
		myapply = lambda x, y: x(*y)
	else:
		logging.info("work division:\n%s", "\n".join("    %s: %r" % kv
			for kv in sorted(dict(numchunks=len(work),
				numproc=numproc).items())))
		# start worker processes
		pool = Pool(processes=numproc, initializer=initworkersimple,
			initargs=(trees, list(sents)))
		mymap = pool.map
		myapply = pool.apply
	# collect recurring fragments
	logging.info("extracting recurring fragments")
	for a in mymap(worker, work):
		fragments.update(a)
	# add 'cover' fragments corresponding to single productions
	cover = myapply(coverfragworker, ())
	before = len(fragments)
	fragments.update(cover)
	logging.info("merged %d unseen cover fragments", len(fragments) - before)
	items = list(fragments.items())
	fragmentkeys = [a for a, _ in items]
	bitsets = [a for _, a in items]
	countchunk = len(bitsets) // numproc + 1
	work = list(range(0, len(bitsets), countchunk))
	work = [(n, len(work), bitsets[a:a + countchunk])
			for n, a in enumerate(work)]
	logging.info("getting exact counts for %d fragments", len(bitsets))
	counts = []
	for a in mymap(exactcountworker, work):
		counts.extend(a)
	if numproc != 1:
		pool.terminate()
		pool.join()
		del pool
	if iterate: # optionally collect fragments of fragments
		from tree import Tree
		logging.info("extracting fragments of recurring fragments")
		params['complement'] = False #needs to be turned off if it was on
		newfrags = fragments
		trees, sents = None, None
		ids = count()
		for _ in range(10): # up to 10 iterations
			newtrees = [binarize(
					introducepreterminals(Tree.parse(tree, parse_leaf=int),
					ids=ids), childchar="}") for tree, _ in newfrags]
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

def iteratefragments(fragments, newtrees, newsents, trees, sents, numproc):
	""" Get fragments of fragments. """
	numtrees = len(newtrees)
	assert numtrees
	if numproc == 1: # set fragments as input
		initworkersimple(newtrees, newsents, trees, sents)
		mymap = map
	else:
		# since the input trees change, we need a new pool each time
		pool = Pool(processes=numproc, initializer=initworkersimple,
			initargs=(newtrees, newsents, trees, sents))
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
		pool.terminate()
		pool.join()
		del pool
	return newkeys, counts

FRONTIERRE = re.compile(r"\(([^ ()]+) \)")
TERMRE = re.compile(r"\(([^ ()]+) ([^ ()]+)\)")
def altrepr(a):
	""" Alternative format
	Replace double quotes with double single quotes: " -> ''
	Quote terminals with double quotes terminal: -> "terminal"
	Remove parentheses around frontier nodes: (NN ) -> NN

	>>> altrepr("(NP (DT a) (NN ))")
	'(NP (DT "a") NN)'
	"""
	return FRONTIERRE.sub(r'\1', TERMRE.sub(r'(\1 "\2")', a.replace('"', "''")))

def printfragments(fragments, out=sys.stdout):
	""" Dump fragments to standard output or some other file object. """
	logging.info("number of fragments: %d", len(fragments))
	if params['nofreq']:
		for a, _ in fragments:
			if params['alt']:
				a = altrepr(a)
			out.write("%s\n" % (("%s\t%s" % (a[0],
					" ".join("%s" % x if x else "" for x in a[1])))
					if params['disc'] else a.decode('utf-8')))
		return
	# when comparing two treebanks, a frequency of 1 is normal;
	# otherwise, raise alarm.
	if params.get('trees2') or params['cover'] or params['complement']:
		threshold = 0
	else:
		threshold = 1
	if params['indices']:
		for a, theindices in fragments:
			if params['alt']:
				a = altrepr(a)
			if len(theindices) > threshold:
				out.write("%s\t%r\n" % ( ("%s\t%s" % (a[0],
					" ".join("%s" % x if x else "" for x in a[1])))
					if params['disc'] else a.decode('utf-8'),
					list(sorted(theindices.elements()))))
			elif threshold:
				logging.warning("invalid fragment--frequency=1: %r", a)
		return
	for a, freq in fragments:
		if freq > threshold:
			if params['alt']:
				a = altrepr(a)
			out.write("%s\t%d\n" % (("%s\t%s" % (a[0],
				" ".join("%s" % x if x else "" for x in a[1])))
				if params['disc'] else a.decode('utf-8'), freq))
		elif threshold:
			logging.warning("invalid fragment--frequency=1: %r", a)

def test():
	""" Simple test. """
	main("fragments.py --disc --encoding iso-8859-1 sample2.export".split())

if __name__ == '__main__':
	main()

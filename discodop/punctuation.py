"""Punctuation related functions."""
from __future__ import division, print_function, absolute_import, \
		unicode_literals
from discodop.tree import Tree, ParentedTree


# fixme: treebank specific parameters for detecting punctuation.
PUNCTTAGS = {"''", '``', '-LRB-', '-RRB-', '.', ':', ',',  # PTB
		'$,', '$.', '$[', '$(',  # Negra/Tiger
		'let', 'LET[]', 'SPEC[symb]', 'TW[hoofd,vrij]',  # Alpino/Lassy
		'COMMA', 'PUNCT', 'PAREN'}  # Grammatical Framework

# NB: ' is not in this list of tokens, because if it occurs as a possesive
# marker it should be left alone; occurrences of ' as quotation marker may
# still be identified using tags.
PUNCTUATION = frozenset('.,():-";?/!*&`[]<>{}|=\xc2\xab\xc2\xbb\xb7\xad\\'
		) | {'&bullet;', '..', '...', '....', '.....', '......',
		'!!', '!!!', '??', '???', "''", '``', ',,',
		'--', '---', '----', '-LRB-', '-RRB-', '-LCB-', '-RCB-'}

# Punctuation that is pruned if it is leading or ending (as in Collins 1999)
PRUNEPUNCT = {'``', "''", '"', '.'}

# Punctuation that come in pairs (left: right).
BALANCEDPUNCTMATCH = {'"': '"', "'": "'", '``': "''",
		'[': ']', '(': ')', '-LRB-': '-RRB-', '-LSB-': '-RSB-',
		'-': '-', '\xc2\xab': '\xc2\xbb'}  # unicode << and >>


def ispunct(word, tag):
	"""Test whether a word and/or tag is punctuation."""
	return tag in PUNCTTAGS or word in PUNCTUATION


def punctremove(tree, sent):
	"""Remove any punctuation nodes, and any empty ancestors."""
	from discodop.treebank import removeterminals
	removeterminals(tree, sent, ispunct)


def punctprune(tree, sent):
	"""Remove quotes and period at sentence beginning and end."""
	from discodop.treebank import removeterminals
	i = 0
	while i < len(sent) and sent[i] in PRUNEPUNCT:
		sent[i] = None
		i += 1
	i = len(sent) - 1
	while i < len(sent) and sent[i] in PRUNEPUNCT:
		sent[i] = None
		i -= 1
	if tree is None:
		sent[:] = [a for a in sent if a is not None]
	else:
		removeterminals(tree, sent, lambda a, _b: a is None)


def punctroot(tree, sent):
	"""Move punctuation directly under ROOT, as in the Negra annotation."""
	punct = []
	for a in reversed(tree.treepositions('leaves')):
		if ispunct(sent[tree[a]], tree[a[:-1]].label):
			# store punctuation node
			punct.append(tree[a[:-1]])
			# remove this punctuation node and any empty ancestors
			for n in range(1, len(a)):
				del tree[a[:-n]]
				if tree[a[:-(n + 1)]]:
					break
	tree.extend(punct)


def punctlower(tree, sent):
	"""Find suitable constituent for punctuation marks and add it there.

	Initial candidate is the root node. Note that ``punctraise()`` performs
	better. Based on rparse code."""
	def lower(node, candidate):
		"""Lower a specific instance of punctuation in tree.

		Recurses top-down on suitable candidates."""
		num = node.leaves()[0]
		for i, child in enumerate(sorted(candidate, key=lambda x: x.leaves())):
			if not child or isinstance(child[0], int):
				continue
			termdom = child.leaves()
			if num < min(termdom):
				candidate.insert(i + 1, node)
				break
			elif num < max(termdom):
				lower(node, child)
				break

	for a in tree.treepositions('leaves'):
		if ispunct(sent[tree[a]], tree[a[:-1]].label):
			b = tree[a[:-1]]
			del tree[a[:-1]]
			lower(b, tree)


def punctraise(tree, sent, rootpreterms=False):
	"""Attach punctuation nodes to an appropriate constituent.

	Trees in the Negra corpus have punctuation attached to the root;
	i.e., it is not part of the phrase-structure. This function moves the
	punctuation to an appropriate level in the tree. A punctuation node is a
	POS tag with a punctuation terminal. Modifies trees in-place.

	:param rootpreterms: if True, move all preterminals under root,
		instead of only recognized punctuation."""
	# punct = [node for node in tree.subtrees() if isinstance(node[0], int)
	punct = [node for node in tree if node and isinstance(node[0], int)
			and (rootpreterms or ispunct(sent[node[0]], node.label))]
	while punct:
		node = punct.pop()
		while node is not tree and len(node.parent) == 1:
			node = node.parent
		if node is tree:
			continue
		node.parent.pop(node.parent_index)
		phrasalnode = lambda n: n and isinstance(n[0], Tree)
		for candidate in tree.subtrees(phrasalnode):
			# add punctuation mark to highest left/right neighbor
			# if any(node[0] - 1 == max(a.leaves()) for a in candidate):
			if any(node[0] + 1 == min(a.leaves()) for a in candidate):
				candidate.append(node)
				break
		else:
			tree.append(node)


def balancedpunctraise(tree, sent):
	"""Move balanced punctuation ``" ' - ( ) [ ]`` to a common constituent.

	Based on rparse code."""
	assert isinstance(tree, ParentedTree)
	# right punct str as key, mapped to left index as value
	punctmap = {}
	# punctuation indices mapped to preterminal nodes
	termparent = {a[0]: a for a in tree.subtrees()
			if a and isinstance(a[0], int) and ispunct(sent[a[0]], a.label)}
	for terminal in sorted(termparent):
		preterminal = termparent[terminal]
		# do we know the matching punctuation mark for this one?
		if preterminal.label in PUNCTTAGS and sent[terminal] in punctmap:
			right = terminal
			left = punctmap[sent[right]]
			rightparent = preterminal.parent
			leftparent = termparent[left].parent
			if max(leftparent.leaves()) == right - 1:
				node = termparent[right]
				leftparent.append(node.parent.pop(node.parent_index))
			elif min(rightparent.leaves()) == left + 1:
				node = termparent[left]
				rightparent.insert(0, node.parent.pop(node.parent_index))
			if sent[right] in punctmap:
				del punctmap[sent[right]]
		elif (sent[terminal] in BALANCEDPUNCTMATCH
				and preterminal.label in PUNCTTAGS):
			punctmap[BALANCEDPUNCTMATCH[sent[terminal]]] = terminal


__all__ = ['ispunct', 'punctremove', 'punctprune', 'punctroot', 'punctlower',
		'punctraise', 'balancedpunctraise']
"""
parses tabular BLAST -m8 (-format 6 in BLAST+) format
"""

import os
import os.path as op
import sys
import math
import logging

from itertools import groupby
from collections import defaultdict
from optparse import OptionParser

import numpy as np

from jcvi.formats.base import LineFile, must_open
from jcvi.formats.coords import print_stats
from jcvi.formats.sizes import Sizes
from jcvi.utils.range import range_distance
from jcvi.apps.base import ActionDispatcher, debug
debug()


class BlastLine(object):
    __slots__ = ('query', 'subject', 'pctid', 'hitlen', 'nmismatch', 'ngaps', \
                 'qstart', 'qstop', 'sstart', 'sstop', 'evalue', 'score', \
                 'qseqid', 'sseqid', 'qi', 'si', 'orientation')

    def __init__(self, sline):
        args = sline.split("\t")
        self.query = args[0]
        self.subject = args[1]
        self.pctid = float(args[2])
        self.hitlen = int(args[3])
        self.nmismatch = int(args[4])
        self.ngaps = int(args[5])
        self.qstart = int(args[6])
        self.qstop = int(args[7])
        self.sstart = int(args[8])
        self.sstop = int(args[9])
        self.evalue = float(args[10])
        self.score = float(args[11])

        if self.sstart > self.sstop:
            self.sstart, self.sstop = self.sstop, self.sstart
            self.orientation = '-'
        else:
            self.orientation = '+'

    def __repr__(self):
        return "BlastLine('%s' to '%s', eval=%.3f, score=%.1f)" % \
                (self.query, self.subject, self.evalue, self.score)

    def __str__(self):
        args = [getattr(self, attr) for attr in BlastLine.__slots__[:12]]
        if self.orientation == '-':
            args[8], args[9] = args[9], args[8]
        return "\t".join(str(x) for x in args)

    @property
    def swapped(self):
        """
        Swap query and subject.
        """
        args = [getattr(self, attr) for attr in BlastLine.__slots__[:12]]
        args[0:2] = [self.subject, self.query]
        args[6:10] = [self.sstart, self.sstop, self.qstart, self.qstop]
        if self.orientation == '-':
            args[8], args[9] = args[9], args[8]
        return "\t".join(str(x) for x in args)

    @property
    def bedline(self):
        return "\t".join(str(x) for x in \
                (self.subject, self.sstart - 1, self.sstop, self.query,
                 self.score, self.orientation))

    def overlap(self, qsize, ssize, max_hang=100, graphic=True, qreverse=False):
        """
        Determine the type of overlap given query, ref alignment coordinates
        Consider the following alignment between sequence a and b:

        aLhang \              / aRhang
                \------------/
                /------------\
        bLhang /              \ bRhang

        Terminal overlap: a before b, b before a
        Contain overlap: a in b, b in a
        """
        aLhang, aRhang = self.qstart - 1, qsize - self.qstop
        bLhang, bRhang = self.sstart - 1, ssize - self.sstop
        if self.orientation == '-':
            bLhang, bRhang = bRhang, bLhang

        if qreverse:
            aLhang, aRhang = aRhang, aLhang
            bLhang, bRhang = bRhang, bLhang

        s1 = aLhang + bRhang
        s2 = aRhang + bLhang
        s3 = aLhang + aRhang
        s4 = bLhang + bRhang

        # >>>>>>>>>>>>>>>>>>>             seqA (alen)
        #           ||||||||
        #          <<<<<<<<<<<<<<<<<<<<<  seqB (blen)
        if graphic:
            achar = ">"
            bchar = "<" if self.orientation == '-' else ">"
            if qreverse:
                achar = "<"
                bchar = {">" : "<", "<" : ">"}[bchar]

            print >> sys.stderr, aLhang, aRhang, bLhang, bRhang
            width = 50  # Canvas
            hitlen = self.hitlen
            lmax = max(aLhang, bLhang)
            rmax = max(aRhang, bRhang)
            bpwidth = lmax + hitlen + rmax
            ratio = width * 1. / bpwidth
            aid = self.query
            bid = self.subject

            # Genbank IDs
            if aid.count("|") >= 3:
                aid = aid.split("|")[3]
            if bid.count("|") >= 3:
                bid = bid.split("|")[3]

            _ = lambda x: int(round(x * ratio, 0))
            a1, a2 = _(aLhang), _(aRhang)
            b1, b2 = _(bLhang), _(bRhang)
            hit = max(_(hitlen), 1)

            msg = " " * max(b1 - a1, 0)
            msg += achar * (a1 + hit + a2)
            msg += " " * (width - len(msg) + 2)
            msg += "{0} ({1})".format(aid, qsize)
            print >> sys.stderr, msg

            msg = " " * max(a1, b1)
            msg += "|" * hit
            print >> sys.stderr, msg

            msg = " " * max(a1 - b1, 0)
            msg += bchar * (b1 + hit + b2)
            msg += " " * (width - len(msg) + 2)
            msg += "{0} ({1})".format(bid, ssize)
            print >> sys.stderr, msg

        # Dovetail (terminal) overlap
        if s1 < max_hang:
            type = 2  # b ~ a
        elif s2 < max_hang:
            type = 1  # a ~ b
        # Containment overlap
        elif s3 < max_hang:
            type = 3  # a in b
        elif s4 < max_hang:
            type = 4  # b in a
        else:
            type = 0

        return type


class BlastSlow (LineFile):
    """
    Load entire blastfile into memory
    """
    def __init__(self, filename):
        super(BlastSlow, self).__init__(filename)
        fp = open(filename)
        for row in fp:
            self.append(BlastLine(row))
        self.sort(key=lambda x: x.query)

    def iter_hits(self):
        for query, blines in groupby(self, key=lambda x: x.query):
            yield query, blines


class Blast (LineFile):
    """
    We can have a Blast class that loads entire file into memory, this is
    not very efficient for big files (BlastSlow); when the BLAST file is
    generated by BLAST/BLAT, the file is already sorted
    """
    def __init__(self, filename):
        super(Blast, self).__init__(filename)
        self.fp = must_open(filename)

    def iter_line(self):
        for row in self.fp:
            yield BlastLine(row)

    def iter_hits(self):
        for query, blines in groupby(self.fp,
                key=lambda x: BlastLine(x).query):
            blines = [BlastLine(x) for x in blines]
            blines.sort(key=lambda x: -x.score)  # descending score
            yield query, blines

    def iter_best_hit(self, N=1):
        for query, blines in groupby(self.fp,
                key=lambda x: BlastLine(x).query):
            blines = [BlastLine(x) for x in blines]
            blines.sort(key=lambda x: -x.score)
            for x in blines[:N]:
                yield query, x

    @property
    def hits(self):
        """
        returns a dict with query => blastline
        """
        return dict(self.iter_hits())

    @property
    def best_hits(self):
        """
        returns a dict with query => best blasthit
        """
        return dict(self.iter_best_hit())


def get_stats(blastfile):

    from jcvi.utils.range import range_union

    logging.debug("report stats on `%s`" % blastfile)
    fp = open(blastfile)
    ref_ivs = []
    qry_ivs = []
    identicals = 0
    alignlen = 0

    for row in fp:
        c = BlastLine(row)
        qstart, qstop = c.qstart, c.qstop
        if qstart > qstop:
            qstart, qstop = qstop, qstart
        qry_ivs.append((c.query, qstart, qstop))

        sstart, sstop = c.sstart, c.sstop
        if sstart > sstop:
            sstart, sstop = sstop, sstart
        ref_ivs.append((c.subject, sstart, sstop))

        alen = sstop - sstart
        alignlen += alen
        identicals += c.pctid / 100. * alen

    qrycovered = range_union(qry_ivs)
    refcovered = range_union(ref_ivs)
    id_pct = identicals * 100. / alignlen

    return qrycovered, refcovered, id_pct


def filter(args):
    """
    %prog filter test.blast

    Produce a new blast file and filter based on score.
    """
    p = OptionParser(filter.__doc__)
    p.add_option("--score", dest="score", default=0, type="int",
            help="Score cutoff [default: %default]")
    p.add_option("--pctid", dest="pctid", default=95, type="int",
            help="Percent identity cutoff [default: %default]")
    p.add_option("--hitlen", dest="hitlen", default=100, type="int",
            help="Hit length cutoff [default: %default]")

    opts, args = p.parse_args(args)
    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    fp = must_open(blastfile)

    score, pctid, hitlen = opts.score, opts.pctid, opts.hitlen
    newblastfile = blastfile + ".P{0}L{1}".format(pctid, hitlen)
    fw = must_open(newblastfile, "w")
    for row in fp:
        if row[0] == '#':
            continue
        c = BlastLine(row)

        if c.score < score:
            continue
        if c.pctid < pctid:
            continue
        if c.hitlen < hitlen:
            continue

        print >> fw, row.rstrip()

    return newblastfile


def main():

    actions = (
        ('summary', 'provide summary on id% and cov%'),
        ('filter', 'filter BLAST file (based on score, id%, alignlen)'),
        ('covfilter', 'filter BLAST file (based on id% and cov%)'),
        ('best', 'get best BLAST hit per query'),
        ('pairs', 'print paired-end reads of BLAST tabular output'),
        ('bed', 'get bed file from blast'),
        ('swap', 'swap query and subjects in the BLAST report'),
        ('mismatches', 'print out histogram of mismatches of HSPs'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def mismatches(args):
    """
    %prog mismatches blastfile

    Print out histogram of mismatches of HSPs, usually for evaluating SNP level.
    """
    from jcvi.utils.cbook import percentage
    from jcvi.graphics.histogram import stem_leaf_plot

    p = OptionParser(mismatches.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    data = []
    b = Blast(blastfile)
    for query, bline in b.iter_best_hit():
        mm = bline.nmismatch + bline.ngaps
        data.append(mm)

    nonzeros = [x for x in data if x != 0]
    title = "Polymorphic sites: {0}".\
            format(percentage(len(nonzeros), len(data)))
    stem_leaf_plot(data, 0, 20, 20, title=title)


def covfilter(args):
    """
    %prog covfilter blastfile fastafile

    Fastafile is used to get the sizes of the queries. Two filters can be
    applied, the id% and cov%.
    """
    p = OptionParser(covfilter.__doc__)
    p.add_option("--pctid", dest="pctid", default=90, type="int",
            help="Percentage identity cutoff [default: %default]")
    p.add_option("--pctcov", dest="pctcov", default=50, type="int",
            help="Percentage identity cutoff [default: %default]")
    p.add_option("--ids", dest="ids", default=None,
            help="Print out the ids that satisfy [default: %default]")
    p.add_option("--list", dest="list", default=False, action="store_true",
            help="List the id% and cov% per gene [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    from jcvi.algorithms.supermap import supermap

    blastfile, fastafile = args
    sizes = Sizes(fastafile).mapping
    querysupermap = blastfile + ".query.supermap"
    if not op.exists(querysupermap):
        supermap(blastfile, filter="query")

    blastfile = querysupermap
    assert op.exists(blastfile)

    covered = 0
    mismatches = 0
    gaps = 0
    alignlen = 0
    queries = set()
    valid = set()
    blast = BlastSlow(querysupermap)
    for query, blines in blast.iter_hits():
        blines = list(blines)
        queries.add(query)

        # per gene report
        this_covered = 0
        this_alignlen = 0
        this_mismatches = 0
        this_gaps = 0

        for b in blines:
            this_covered += abs(b.qstart - b.qstop + 1)
            this_alignlen += b.hitlen
            this_mismatches += b.nmismatch
            this_gaps += b.ngaps

        this_identity = 100. - (this_mismatches + this_gaps) * 100. / this_alignlen
        this_coverage = this_covered * 100. / sizes[query]

        if opts.list:
            print "{0}\t{1:.1f}\t{2:.1f}".format(query, this_identity, this_coverage)

        if this_identity >= opts.pctid and this_coverage >= opts.pctcov:
            valid.add(query)

        covered += this_covered
        mismatches += this_mismatches
        gaps += this_gaps
        alignlen += this_alignlen

    mapped_count = len(queries)
    valid_count = len(valid)
    cutoff_message = "(id={0.pctid}% cov={0.pctcov}%)".format(opts)

    print >> sys.stderr, "Identity: {0} mismatches, {1} gaps, {2} alignlen".\
            format(mismatches, gaps, alignlen)
    total = len(sizes.keys())
    print >> sys.stderr, "Total mapped: {0} ({1:.1f}% of {2})".\
            format(mapped_count, mapped_count * 100. / total, total)
    print >> sys.stderr, "Total valid {0}: {1} ({2:.1f}% of {3})".\
            format(cutoff_message, valid_count, valid_count * 100. / total, total)
    print >> sys.stderr, "Id % = {0:.2f}%".\
            format(100 - (mismatches + gaps) * 100. / alignlen)

    queries_combined = sum(sizes[x] for x in queries)
    print >> sys.stderr, "Coverage: {0} covered, {1} total".\
            format(covered, queries_combined)
    print >> sys.stderr, "Coverage = {0:.2f}%".\
            format(covered * 100. / queries_combined)

    if opts.ids:
        filename = opts.ids
        fw = must_open(filename, "w")
        for id in valid:
            print >> fw, id
        logging.debug("Queries beyond cutoffs {0} written to `{1}`.".\
                format(cutoff_message, filename))


def swap(args):
    """
    %prog swap blastfile

    Print out a new blast file with query and subject swapped.
    """
    p = OptionParser(swap.__doc__)

    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(p.print_help())

    blastfile = args
    fp = must_open(blastfile)
    swappedblastfile = blastfile + ".swapped"
    fw = must_open(swappedblastfile)
    for row in fp:
        b = BlastLine(row)
        print >> fw, b.swapped


def bed(args):
    """
    %prog bed blastfile

    Print out a bed file based on the coordinates in BLAST report.
    """
    p = OptionParser(bed.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    blastfile, = args
    fp = must_open(blastfile)
    bedfile = blastfile.rsplit(".", 1)[0] + ".bed"
    fw = open(bedfile, "w")
    for row in fp:
        b = BlastLine(row)
        print >> fw, b.bedline

    logging.debug("File written to `{0}`.".format(bedfile))

    return bedfile


def set_options_pairs():
    """
    %prog pairs <blastfile|casfile|bedfile|posmapfile>

    Report how many paired ends mapped, avg distance between paired ends, etc.

    Reads have to be have the same prefix, use --rclip to remove trailing
    part, e.g. /1, /2, or .f, .r.
    """
    p = OptionParser(set_options_pairs.__doc__)

    p.add_option("--cutoff", dest="cutoff", default=0, type="int",
            help="distance to call valid links between mates "\
                 "[default: estimate from input]")
    p.add_option("--mateorientation", default=None,
            choices=("++", "--", "+-", "-+"),
            help="use only certain mate orientations [default: %default]")
    p.add_option("--pairs", dest="pairsfile",
            default=False, action="store_true",
            help="write valid pairs to pairsfile")
    p.add_option("--inserts", dest="insertsfile", default=True,
            help="write insert sizes to insertsfile and plot distribution " + \
            "to insertsfile.pdf")
    p.add_option("--nrows", default=100000, type="int",
            help="only use the first n lines [default: %default]")
    p.add_option("--rclip", default=1, type="int",
            help="pair ID is derived from rstrip N chars [default: %default]")
    p.add_option("--pdf", default=False, action="store_true",
            help="print PDF instead ASCII histogram [default: %default]")
    p.add_option("--bins", default=20, type="int",
            help="number of bins in the histogram [default: %default]")

    return p


def report_pairs(data, cutoff=0, mateorientation=None,
        pairsfile=None, insertsfile=None, rclip=1, ascii=False, bins=20):
    """
    This subroutine is used by the pairs function in blast.py and cas.py.
    Reports number of fragments and pairs as well as linked pairs
    """
    allowed_mateorientations = ("++", "--", "+-", "-+")

    if mateorientation:
        assert mateorientation in allowed_mateorientations

    num_fragments, num_pairs = 0, 0

    all_dist = []
    linked_dist = []
    # +- (forward-backward) is `innie`, -+ (backward-forward) is `outie`
    orientations = defaultdict(int)

    # clip how many chars from end of the read name to get pair name
    key = lambda x: x.accn[:-rclip] if rclip else x.accn
    data.sort(key=key)

    if pairsfile:
        pairsfw = open(pairsfile, "w")
    if insertsfile:
        insertsfw = open(insertsfile, "w")

    for pe, lines in groupby(data, key=key):
        lines = list(lines)
        if len(lines) != 2:
            num_fragments += len(lines)
            continue

        num_pairs += 1
        a, b = lines

        asubject, astart, astop = a.seqid, a.start, a.end
        bsubject, bstart, bstop = b.seqid, b.start, b.end

        aquery, bquery = a.accn, b.accn
        astrand, bstrand = a.strand, b.strand

        dist, orientation = range_distance(\
                (asubject, astart, astop, astrand),
                (bsubject, bstart, bstop, bstrand))

        if dist >= 0:
            all_dist.append((dist, orientation, aquery, bquery))

    # try to infer cutoff as twice the median until convergence
    if cutoff <= 0:
        if mateorientation:
            dists = np.array([x[0] for x in all_dist \
                    if x[1] == mateorientation], dtype="int")
        else:
            dists = np.array([x[0] for x in all_dist], dtype="int")

        p0 = np.median(dists)
        cutoff = int(2 * p0)  # initial estimate
        cutoff = int(math.ceil(cutoff / bins)) * bins
        logging.debug("Insert size cutoff set to {0}, ".format(cutoff) +
            "use '--cutoff' to override")

    for dist, orientation, aquery, bquery in all_dist:
        if dist > cutoff:
            continue

        linked_dist.append(dist)
        if pairsfile:
            print >> pairsfw, "{0}\t{1}\t{2}".format(aquery, bquery, dist)
        orientations[orientation] += 1

    print >>sys.stderr, "%d fragments, %d pairs" % (num_fragments, num_pairs)
    num_links = len(linked_dist)

    linked_dist = np.array(linked_dist, dtype="int")
    linked_dist = np.sort(linked_dist)

    meandist = np.mean(linked_dist)
    stdev = np.std(linked_dist)

    p0 = np.median(linked_dist)
    p1 = linked_dist[int(num_links * .025)]
    p2 = linked_dist[int(num_links * .975)]

    meandist, stdev = int(meandist), int(stdev)
    p0 = int(p0)

    print >>sys.stderr, "%d pairs (%.1f%%) are linked (cutoff=%d)" % \
            (num_links, num_links * 100. / num_pairs, cutoff)

    print >>sys.stderr, "mean distance between mates: {0} +/- {1}".\
            format(meandist, stdev)
    print >>sys.stderr, "median distance between mates: {0}".format(p0)
    print >>sys.stderr, "95% distance range: {0} - {1}".format(p1, p2)
    print >>sys.stderr, "\nOrientations:"

    orientation_summary = []
    for orientation, count in sorted(orientations.items()):
        o = "{0}:{1}".format(orientation, count)
        orientation_summary.append(o)
        print >>sys.stderr, o

    if insertsfile:
        from jcvi.graphics.histogram import histogram

        print >>insertsfw, "\n".join(str(x) for x in linked_dist)
        insertsfw.close()
        prefix = insertsfile.rsplit(".", 1)[0]
        osummary = " ".join(orientation_summary)
        title="{0} ({1}; median dist:{2})".format(prefix, osummary, p0)
        histogram(insertsfile, vmin=0, vmax=cutoff, bins=bins,
                xlabel="Insertsize", title=title, ascii=ascii)
        os.remove(insertsfile)

    return meandist, stdev, p0, p1, p2


def pairs(args):
    """
    See __doc__ for set_options_pairs().
    """
    import jcvi.formats.bed

    p = set_options_pairs()

    opts, targs = p.parse_args(args)

    if len(targs) != 1:
        sys.exit(not p.print_help())

    blastfile, = targs
    bedfile = bed([blastfile])
    args[args.index(blastfile)] = bedfile

    return jcvi.formats.bed.pairs(args)


def best(args):
    """
    %prog best blastfile

    print the best hit for each query in the blastfile
    """
    p = OptionParser(best.__doc__)

    p.add_option("-N", dest="N", default=1, type="int",
            help="get best N hits [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    bestblastfile = blastfile + ".best"
    fw = open(bestblastfile, "w")

    b = Blast(blastfile)
    for q, bline in b.iter_best_hit(N=opts.N):
        print >> fw, bline


def summary(args):
    """
    %prog summary blastfile

    Provide summary on id% and cov%, for both query and reference. Often used in
    comparing genomes (based on NUCMER results).
    """
    p = OptionParser(summary.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    blastfile, = args

    qrycovered, refcovered, id_pct = get_stats(blastfile)
    print_stats(qrycovered, refcovered, id_pct)


if __name__ == '__main__':
    main()

import locale
import logging
import math
import os.path
import tempfile
from io import StringIO

import numpy as np
import pandas as pd
from pegeno import tabio
from pegeno.intersect import iter_slices

from .. import kernel, multiprocess, hyperparameters, adjusting, variation
from ..cnv import CopyNumArray as CNA
from ..filteringSegm import squash_by_groups
from . import cbs, flasso, haar, hmm, none
import matplotlib
from matplotlib import pyplot
# matplotlib.use('Qt5Agg')
matplotlib.use('Agg')
SEGMENT_METHODS = ('cbs', 'flasso', 'haar', 'none',
                   'hmm', 'hmm-tumor', 'hmm-germline')


def do_segmentation(cnarr, method, threshold=None, variants=None,
                    skip_low=False, skip_outliers=10, min_weight=0,
                    save_dataframe=False, rscript_path="Rscript",
                    processes=1, smooth_cbs=False):
    if method not in SEGMENT_METHODS:
        raise ValueError("'method' must be one of: "
                         + ", ".join(SEGMENT_METHODS)
                         + "; got: " + repr(method))

    if not threshold:
        threshold = {'cbs': 0.0001,
                     'flasso': 0.0001,
                     'haar': 0.0001,
                    }.get(method)
    msg = "Segmenting with method " + repr(method)
    if threshold is not None:
        if method.startswith('hmm'):
            msg += ", adjusting window size %s," % threshold
        else:
            msg += ", significance threshold %s," % threshold
    msg += " in %s processes" % processes
    logging.info(msg)

    if method == 'flasso' or method.startswith('hmm'):
        cna = _do_segmentation(cnarr, method, threshold, variants, skip_low,
                               skip_outliers, min_weight, save_dataframe,
                               rscript_path)
        if save_dataframe:
            cna, rstr = cna
            rstr = _to_str(rstr)

    else:
        with multiprocess.pick_pool(processes) as pool:
            rets = list(pool.map(_ds, ((ca, method, threshold, variants,
                                        skip_low, skip_outliers, min_weight,
                                        save_dataframe, rscript_path, smooth_cbs)
                                       for _, ca in cnarr.by_arm())))
        if save_dataframe:
            rets, r_dframe_strings = zip(*rets)
            r_dframe_strings = map(_to_str, r_dframe_strings)
            rstr = [next(r_dframe_strings)]
            rstr.extend(r[r.index('\n') + 1:] for r in r_dframe_strings)
            rstr = "".join(rstr)
        cna = cnarr.concat(rets)

    cna.sort_columns()
    if save_dataframe:
        return cna, rstr
    return cna


def _to_str(s, enc=locale.getpreferredencoding()):
    if isinstance(s, bytes):
        return s.decode(enc)
    return s


def _ds(args):
    return _do_segmentation(*args)


def _do_segmentation(cnarr, method, threshold, variants=None,
                     skip_low=False, skip_outliers=10, min_weight=0,
                     save_dataframe=False,
                     rscript_path="Rscript", smooth_cbs=False):
    if not len(cnarr):
        return cnarr

    filtered_cn = cnarr.copy()
    if skip_low:
        filtered_cn = filtered_cn.drop_low_coverage(verbose=False)
    if skip_outliers:
        filtered_cn = drop_outliers(filtered_cn, 50, skip_outliers)
    if min_weight:
        weight_too_low = (filtered_cn["weight"] < min_weight).fillna(True)
    else:
        weight_too_low = (filtered_cn["weight"] == 0).fillna(True)
    n_weight_too_low = weight_too_low.sum() if len(weight_too_low) else 0
    if n_weight_too_low:
        filtered_cn = filtered_cn[~weight_too_low]
        if min_weight:
            logging.debug("Dropped %d bins with weight below %s",
                          n_weight_too_low, min_weight)
        else:
            logging.debug("Dropped %d bins with zero weight",
                          n_weight_too_low)

    if len(filtered_cn) != len(cnarr):
        msg = ("Dropped %d / %d bins"
               % (len(cnarr) - len(filtered_cn), len(cnarr)))
        if cnarr["chromosome"].iat[0] == cnarr["chromosome"].iat[-1]:
            msg += " on chromosome " + str(cnarr["chromosome"].iat[0])
        logging.info(msg)
    if not len(filtered_cn):
        return filtered_cn

    seg_out = ""
    if method == 'haar':
        segarr = haar.segment_haar(filtered_cn, threshold)

    elif method == 'none':
        segarr = none.segment_none(filtered_cn)

    elif method.startswith('hmm'):
        segarr = hmm.segment_hmm(filtered_cn, method, threshold, variants)

    elif method in ('cbs', 'flasso'):
        rscript = {'cbs': cbs.CBS_RSCRIPT,
                   'flasso': flasso.FLASSO_RSCRIPT,
                  }[method]
        data1 = filtered_cn[filtered_cn['chromosome'] == '1']
        dfarr1 = data1['log2'].ewm(span=10).mean()
        pedata = data1['log2']
        indices = np.arange(len(pedata))
        pyplot.scatter(indices, pedata, alpha=0.2, color='gray')
        pyplot.scatter(indices, dfarr1, alpha=0.2, color='red')
        filtered_cn['log2'] = filtered_cn['log2'].ewm(span=3).mean()
        filtered_cn['start'] += 1
        with tempfile.NamedTemporaryFile(suffix='.cnr', mode="w+t") as tmp:
            filtered_cn.data.to_csv(tmp, index=False, sep='\t',
                                    float_format='%.6g', mode="w+t")
            tmp.flush()
            script_strings = {
                'probes_fname': tmp.name,
                'sample_id': cnarr.sample_id,
                'threshold': threshold,
                'smooth_cbs': smooth_cbs
            }
            with kernel.temp_write_text(rscript % script_strings,
                                        mode='w+t') as script_fname:
                seg_out = kernel.call_quiet(rscript_path,
                                          "--no-restore",
                                          "--no-environ",
                                            script_fname)
        segarr = tabio.read(StringIO(seg_out.decode()), "seg", into=CNA)
        if method == 'flasso':
            if 'weight' in filtered_cn:
                segarr['weight'] = filtered_cn['weight']
            else:
                segarr['weight'] = 1.0
            segarr = squash_by_groups(segarr, segarr['log2'], by_arm=True)

    else:
        raise ValueError("Unknown method %r" % method)

    segarr.meta = cnarr.meta.copy()
    if variants and not method.startswith('hmm'):
        
        logging.info("Re-segmenting on variant allele frequency")
        newsegs = [hmm.variants_in_segment(subvarr, segment)
                   for segment, subvarr in variants.by_ranges(segarr)]
        segarr = segarr.as_dataframe(pd.concat(newsegs))
        segarr['baf'] = variants.baf_by_ranges(segarr)

    segarr = transfer_fields(segarr, cnarr)
    if save_dataframe:
        return segarr, seg_out
    else:
        return segarr


def drop_outliers(cnarr, width, factor):
    if not len(cnarr):
        return cnarr
    outlier_mask = np.concatenate([
        adjusting.rolling_outlier_quantile(subarr['log2'], width, .95, factor)
        for _chrom, subarr in cnarr.by_chromosome()])
    n_outliers = outlier_mask.sum()
    if n_outliers:
        logging.info("Dropped %d outlier bins:\n%s%s",
                     n_outliers,
                     cnarr[outlier_mask].data.head(20),
                     "\n..." if n_outliers > 20 else "")
    return cnarr[~outlier_mask]


def transfer_fields(segments, cnarr, ignore=hyperparameters.IGNORE_GENE_NAMES):
    def make_null_segment(chrom, orig_start, orig_end):
        """Closes over 'segments'."""
        vals = {'chromosome': chrom,
                'start': orig_start,
                'end': orig_end,
                'gene': '-',
                'depth': 0.0,
                'log2': 0.0,
                'probes': 0.0,
                'weight': 0.0,
               }
        row_vals = tuple(vals[c] for c in segments.data.columns)
        return row_vals

    if not len(cnarr):
        logging.warn("No bins for:\n%s", segments.data)
        return segments

    bins_chrom = cnarr.chromosome.iat[0]
    bins_start = cnarr.start.iat[0]
    bins_end = cnarr.end.iat[-1]
    if not len(segments):
        return make_null_segment(bins_chrom, bins_start, bins_end)
    segments.start.iat[0] = bins_start
    segments.end.iat[-1] = bins_end

    ignore += hyperparameters.OFFTARGET_ALIASES
    assert bins_chrom == segments.chromosome.iat[0]
    cdata = cnarr.data.reset_index()
    if 'depth' not in cdata.columns:
        cdata['depth'] = np.exp2(cnarr['log2'].values)
    bin_genes = cdata['gene'].values
    bin_weights = cdata['weight'].values if 'weight' in cdata.columns else None
    bin_depths = cdata['depth'].values
    seg_genes = ['-'] * len(segments)
    seg_weights = np.zeros(len(segments))
    seg_depths = np.zeros(len(segments))

    for i, bin_idx in enumerate(iter_slices(cdata, segments.data, 'outer', False)):
        if bin_weights is not None:
            seg_wt = bin_weights[bin_idx].sum()
            if seg_wt > 0:
                seg_dp = np.average(bin_depths[bin_idx],
                                    weights=bin_weights[bin_idx])
            else:
                seg_dp = 0.0
        else:
            bin_count = len(cdata.iloc[bin_idx])
            seg_wt = float(bin_count)
            seg_dp = bin_depths[bin_idx].mean()
        subgenes = [g for g in pd.unique(bin_genes[bin_idx]) if g not in ignore]
        if subgenes:
            seg_gn = ",".join(subgenes)
        else:
            seg_gn = '-'
        seg_genes[i] = seg_gn
        seg_weights[i] = seg_wt
        seg_depths[i] = seg_dp

    segments.data = segments.data.assign(
        gene=seg_genes,
        weight=seg_weights,
        depth=seg_depths)
    return segments

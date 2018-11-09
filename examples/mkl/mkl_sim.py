hlp = """
    Trajectory of grpup LARS with different penalty functions 
    and KMP greedy algorithm.
"""

import itertools as it
import numpy as np
import csv
import os
os.environ["OCTAVE_EXECUTABLE"] = "/usr/local/bin/octave"

# Kernels
from mklaren.kernel.kernel import exponential_kernel, kernel_sum
from mklaren.kernel.kinterface import Kinterface
from mklaren.regression.ridge import RidgeLowRank
from mklaren.mkl.kmp import KMP
from mklaren.regression.ridge import RidgeMKL

# New methods
from examples.lars.lars_mkl import LarsMKL
from examples.lars.lars_group import p_ri, p_const, p_sc, p_sig, p_act
from examples.mkl.mkl_est_var import estimate_sigma_dist
from scipy.stats import multivariate_normal as mvn

# Parameters
out_dir = "/Users/martins/Dev/mklaren/examples/mkl/results/mkl_sim"
N = 1000
delta = 5
p_tr = .8
lbd = 0.000
rank = 30

formats = {"lars-ri": "gv-",
           "lars-co": "cv-",
           "lars-sig": "bv-",
           "lars-act": "yv-",
           # "lars-sc": "rv-",
           # "kmp": "c--",
           # "icd": "b--",
           # "nystrom": "m--",
           # "csi": "r--",
           # "L2KRR": "k-"
           }


header = ["repl", "method", "N", "d", "keff", "sigma", "noise", "snr", "evar", "ranking"]

penalty = {
    "lars-ri": p_ri,
    "lars-sc": p_sc,
    "lars-co": p_const,
    "lars-sig": p_sig,
    "lars-act": p_act,
    }


def process():
    # Load data
    np.random.seed(42)
    noise_range = np.logspace(-5, 2, 8)
    d_range = [1, 10, 100]
    replicates = 30

    rows = list()
    count = 0
    for repl, noise, d in it.product(range(replicates), noise_range, d_range):

        # Ground truth kernels
        X = np.random.randn(N, d)
        sigma_range = estimate_sigma_dist(X, 10)

        Ks = [Kinterface(data=X,
                         kernel=exponential_kernel,
                         kernel_args={"sigma": sigma})
              for sigma in sigma_range]

        # Simulate data from one kernel
        keff = 5
        f = mvn.rvs(mean=np.zeros((N,)), cov=Ks[keff][:, :])
        y = mvn.rvs(mean=f, cov=noise * np.eye(N))
        snr = np.round(np.var(f) / noise, 3)

        # Training/test
        tr = np.sort(np.random.choice(range(N), size=int(p_tr * N), replace=False))
        te = np.sort(np.array(list(set(range(N)) - set(tr))))

        # Training kernels
        Ks_tr = [Kinterface(data=X[tr],
                            kernel=exponential_kernel,
                            kernel_args={"sigma": sigma})
                 for sigma in sigma_range]

        Ksum_tr = Kinterface(data=X[tr],
                             kernel=kernel_sum,
                             kernel_args={"kernels": [exponential_kernel] * len(sigma_range),
                                          "kernels_args": [{"sigma": sig} for sig in sigma_range]})

        # Collect test error paths
        results = dict()
        try:
            for m in formats.keys():
                if m.startswith("lars-"):
                    model = LarsMKL(delta=delta, rank=rank, f=penalty[m])
                    model.fit(Ks_tr, y[tr])
                    ypath = model.predict_path_ls([X[te]] * len(Ks_tr))
                elif m == "kmp":
                    model = KMP(rank=rank, delta=delta, lbd=0)
                    model.fit(Ks_tr, y[tr])
                    ypath = model.predict_path([X[te]] * len(Ks_tr))
                elif m == "icd":
                    model = RidgeLowRank(method="icd",
                                         rank=rank, lbd=0)
                    model.fit([Ksum_tr], y[tr])
                    ypath = model.predict_path([X[te]])
                elif m == "nystrom":
                    model = RidgeLowRank(method="nystrom",
                                         rank=rank, lbd=0)
                    model.fit([Ksum_tr], y[tr])
                    ypath = model.predict_path([X[te]])
                elif m == "csi":
                    model = RidgeLowRank(method="csi", rank=rank,
                                         lbd=0, method_init_args={"delta": delta})
                    model.fit([Ksum_tr], y[tr])
                    ypath = model.predict_path([X[te]])
                elif m == "L2KRR":
                    model = RidgeMKL(method="l2krr", lbd=0)
                    model.fit(Ks=Ks, y=y, holdout=te)
                    ypath = np.vstack([model.predict(te)] * rank).T
                else:
                    raise ValueError(m)

                # Compute explained variance
                evars = np.zeros(ypath.shape[1])
                for j in range(ypath.shape[1]):
                    evars[j] = (np.var(y[te]) - np.var(ypath[:, j] - y[te])) / np.var(y[te])
                results[m] = evars

        except AssertionError:
            continue
        except ValueError as ve:
            print(str(ve) + " Noise: %s" % noise)
            continue

        # Compute ranking
        # scores = dict([(m, np.mean(ev)) for m, ev in results.items()])
        scores = dict([(m, ev[-1]) for m, ev in results.items()])
        scale = np.array(sorted(scores.values(), reverse=True)).ravel()
        for m in results.keys():
            ranking = 1 + np.where(scale == scores[m])[0][0]
            row = {"repl": repl, "method": m, "noise": noise, "N": N,
                   "keff": keff, "sigma": sigma_range[keff], "d": d,
                   "evar": scores[m], "ranking": ranking, "snr": snr}
            rows.append(row)

        # Write to .csv
        fname = os.path.join(out_dir, "results.csv")
        fp = open(fname, "w")
        writer = csv.DictWriter(fp, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
        fp.close()
        print("Written %s (%d)" % (fname, count))
        count += 1


if __name__ == "__main__":
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        print("Makedir %s" % out_dir)
    process()
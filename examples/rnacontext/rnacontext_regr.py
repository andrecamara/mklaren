import sys
import os
import csv
import scipy.stats as st
import datetime
import time
import itertools as it
from mklaren.kernel.string_kernel import *
from mklaren.kernel.string_util import *
from mklaren.kernel.kernel import kernel_sum
from mklaren.kernel.kinterface import Kinterface
from mklaren.mkl.mklaren import Mklaren
from mklaren.regression.ridge import RidgeLowRank
from datasets.rnacontext import load_rna
#
# # List available kernels
# args = [
#     # {"mode": SPECTRUM, "K": 2},
#     # {"mode": SPECTRUM, "K": 3},
#     {"mode": SPECTRUM, "K": 3},
#     {"mode": SPECTRUM, "K": 4},
#     # {"mode": SPECTRUM, "K": 5},
#     # {"mode": SPECTRUM_MISMATCH, "K": 2},
#     {"mode": SPECTRUM_MISMATCH, "K": 3},
#     {"mode": SPECTRUM_MISMATCH, "K": 4},
#     # {"mode": SPECTRUM_MISMATCH, "K": 5},
#     {"mode": WD, "K": 4, "minK": 3},
#     # {"mode": WD_PI, "K": 2},
#     # {"mode": WD_PI, "K": 3},
#     {"mode": WD_PI, "K": 3},
#     {"mode": WD_PI, "K": 4},
#     # {"mode": WD_PI, "K": 5},
# ]
args = [{"mode": SPECTRUM, "K": kl} for kl in range(1, 10)]
kernels = ",".join(set(map(lambda t: t["mode"], args)))

# Load data
dset = dict(enumerate(sys.argv)).get(1, "U1A_data_full_AB.txt.gz")


# Hyperparameters
methods = ["Mklaren", "CSI", "Nystrom", "ICD"]
lbd_range  = [0] + list(np.logspace(-5, 1, 7))  # Regularization parameter
rank_range = (5, 10, 20)
iterations = range(30)
delta = 10
n_tr = 1000
n_val = 1000
# n_te = 1000


# Fixed output
# Create output directory
d = datetime.datetime.now()
dname = os.path.join("..", "output", "rnacontext",
                     "%d-%d-%d" % (d.year, d.month, d.day))
if not os.path.exists(dname): os.makedirs(dname)
rcnt = len(os.listdir(dname))
fname = os.path.join(dname, "results_%d.csv" % rcnt)
print("Writing to %s ..." % fname)

# Output
header = ["dataset", "n", "L", "kernels", "method", "rank", "iteration", "lambda",
          "time", "evar_tr", "evar_va", "evar"]
fp = open(fname, "w", buffering=0)
writer = csv.DictWriter(fp, fieldnames=header)
writer.writeheader()

# Load data
data = load_rna(dset)
X = data["data"]
y = st.zscore(data["target"])
n, L = len(X), len(X[0])

# Generate random datasets and perform prediction
count = 0
seed = 0
for cv in iterations:

    # Select random test/train indices
    inxs = np.arange(n, dtype=int)
    np.random.shuffle(inxs)
    tr = inxs[:n_tr]
    va = inxs[n_tr:n_tr + n_val]
    te = inxs[n_tr + n_val:]

    # Training / test split
    X_tr, y_tr = X[tr], y[tr]
    X_va, y_va = X[va], y[va]
    X_te, y_te = X[te], y[te]

    # Individual and sum of kernels
    Ks = [Kinterface(kernel=string_kernel, data=X_tr, kernel_args=arg) for arg in args]
    Ksum = Kinterface(data=X_tr, kernel=kernel_sum,
                      kernel_args={"kernels": [string_kernel] * len(args),
                                   "kernels_args": args})

    # Modeling
    for method in methods:
        for lbd, rank in it.product(lbd_range, rank_range):
            yt, yv, yp = None, None, None
            t1 = time.time()
            if method == "Mklaren":
                mkl = Mklaren(rank=rank, lbd=lbd, delta=delta)
                try:
                    mkl.fit(Ks, y_tr)
                    yt = mkl.predict([X_tr] * len(Ks))
                    yv = mkl.predict([X_va] * len(Ks))
                    yp = mkl.predict([X_te] * len(Ks))
                except Exception as e:
                    print(e)
                    continue
            else:
                if method == "CSI":
                    model = RidgeLowRank(rank=rank, method="csi",
                                         method_init_args={"delta": delta}, lbd=lbd)
                else:
                    model = RidgeLowRank(rank=rank, method=method.lower(), lbd=lbd)
                try:
                    model.fit([Ksum], y_tr)
                    yt = model.predict([X_tr])
                    yv = model.predict([X_va])
                    yp = model.predict([X_te])
                except Exception as e:
                    print(e)
                    continue
            t2 = time.time() - t1

            # Evaluate explained varaince on the three sets
            evar_tr = (np.var(y_tr) - np.var(yt - y_tr)) / np.var(y_tr)
            evar_va = (np.var(y_va) - np.var(yv - y_va)) / np.var(y_va)
            evar    = (np.var(y_te) - np.var(yp - y_te)) / np.var(y_te)

            row = {"L": L, "n": len(X), "method": method, "dataset": dset,
                   "kernels": kernels, "rank": rank, "iteration": cv, "lambda": lbd,
                   "time": t2, "evar_tr": evar_tr, "evar_va": evar_va, "evar": evar}

            writer.writerow(row)
            seed += 1
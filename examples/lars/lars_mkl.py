import numpy as np
import matplotlib.pyplot as plt
from numpy.linalg import norm, inv
from collections import defaultdict
from scipy.stats import multivariate_normal as mvn
from examples.lars.cholesky import cholesky_steps, cholesky_transform
from examples.lars.lars_group import p_ri, p_const, p_sc, colors
from examples.lars.qr import qr_steps, solve_R
from warnings import warn
from mklaren.util.la import safe_divide as div
from mklaren.kernel.kinterface import Kinterface
from mklaren.kernel.kernel import exponential_kernel
from time import time

hlp = """
    LARS with multiple kernels.
    Use a penalty function to influence the selection of new kernels.
"""


class LarsMKL:

    def __init__(self, rank, delta, f=p_ri):
        self.rank = rank
        self.delta = delta
        self.f = f
        self.Q = None
        self.Qview = None
        self.Qmap = None
        self.Ri = None
        self.R = None
        self.P = None
        self.Pi = None
        self.Acts = None
        self.Ks = None
        self.path = None
        self.mu = None
        self.Ws = None
        self.korder = None

    @staticmethod
    def zy_app(G, Q, y, QTY):
        """ Approximate |<z, y>|^2 for current approximations G, Q.
            QTY is cached for all kernels. """
        n = G.shape[0]
        if G.shape[1] == 0:
            return -np.inf * np.ones((n,))
        GTY = G.T.dot(y)
        GTQ = G.T.dot(Q)
        A1 = GTY - GTQ.dot(QTY)
        A2 = A1.dot(A1.T)
        C = np.array([G[i, :].T.dot(A2).dot(G[i, :]) for i in range(n)])
        A3 = G.T.dot(G) - GTQ.dot(GTQ.T)
        B = np.array([G[i, :].T.dot(A3).dot(G[i, :]) for i in range(n)])
        B = np.round(B, 9)
        return div(C, B)

    def fit(self, Ks, y):
        """ LARS-MKL sampling algorithm with a penalty function. Produces a feature space. """
        assert all([isinstance(K, Kinterface) for K in Ks])
        rank = self.rank
        delta = self.delta
        f = self.f
        y = y.reshape((len(y), 1))

        if delta == 1:
            raise ValueError("Unstable selection of delta. (delta = 1)")

        # Shared variables
        k = len(Ks)
        n = Ks[0].shape[0]
        Gs = []
        Acts = []
        Inas = []

        # Master QR decomposition and index of kernels and Permutation
        Q = np.zeros((n, rank + k * delta))
        R = np.zeros((rank + k * delta, rank + k * delta))
        Pagg = defaultdict(int)     # Number of pivots per kernel
        P = []                      # Order of kernels per selected pivot

        # Current status and costs
        corr = np.zeros((k,))  # Current correlation per kernel || X.T y || (required for calculating gain)
        cost = np.zeros((k,))  # Current derived cost per kernel || X.T y || * f(p)
        ncol = np.zeros((k,))  # Current number of columns per kernel (p)
        gain = np.zeros((k, n))  # Individual column gains

        # Look-ahead phase
        for j in range(len(Ks)):
            Gs.append(np.zeros((n, rank + delta)))
            Acts.append([])
            Inas.append(range(n))

            # Initial look-ahead setup ; one step per kernel
            if delta > 0:
                cholesky_steps(Ks[j], Gs[j], act=[], ina=range(n), max_steps=delta)

        # Iterations to fill the active set
        for step in range(rank):

            # Compute gains
            Qa = Q[:, :step]
            QTY = Qa.T.dot(y)
            for j in range(k):
                if delta > 0:
                    Ga = Gs[j][:, Pagg[j]:Pagg[j]+delta]
                    # assert norm(Ga) > 0
                    zy2 = self.zy_app(Ga, Qa, y, QTY)
                    gain[j, :] = (zy2 > 0) * (zy2 * f(ncol[j] + 1) + (f(ncol[j] + 1) - f(ncol[j])) * corr[j])
                    gain[j, Acts[j]] = -np.inf
                else:
                    gain[j, :] = Ks[j].diagonal() - (Gs[j] ** 2).sum(axis=1).ravel()
                    gain[j, Acts[j]] = -np.inf

            # Select optimal kernel and pivot
            kern, pivot = np.unravel_index(np.argmax(gain), gain.shape)
            if gain[kern, pivot] == 0:
                msg = "Iterations ended prematurely (%s) at step = %d < %d" % (str(f.__name__), step, rank)
                self.rank = step
                raise ValueError(msg)

            # Select pivot and update Cholesky factors; try simplyfing
            G = Gs[kern]
            K = Ks[kern]
            P.append(kern)
            Pagg[kern] = Pagg[kern] + 1
            k_num = Pagg[kern]
            k_inx = np.where(np.array(P) == kern)[0]
            k_start = k_num - 1

            # Update Cholesky
            G[:, k_start:] = 0
            cholesky_steps(K, G, start=k_start, act=Acts[kern], ina=Inas[kern], order=[pivot])
            qr_steps(G, Q, R, max_steps=1, start=k_start, qstart=step)

            # Clear invalid columns and update lookahead steps;
            # Copy act/ina
            max_steps = min(delta, G.shape[1] - k_num)
            cholesky_steps(K, G,
                           act=list(Acts[kern]),
                           ina=list(set(range(n)) - set(Acts[kern])),
                           start=k_num,
                           max_steps=max_steps)

            # Update current correlation and total cost
            ncol[kern] += 1
            corr[kern] = norm(Q[:, k_inx].T.dot(y)) ** 2
            cost[kern] = corr[kern] * f(ncol[kern])

        # Determine order of kernels by cost
        self.korder = np.array(filter(lambda kj: kj in set(P),
                                      np.argsort(-cost).astype(int)))

        # Use reduced approximation and store
        self.Ks = Ks
        self.Acts = Acts
        self.Q = Q[:, :rank]
        self.R = R[:rank, :rank]
        self.Ri = solve_R(R[:rank, :rank])

        # Permutations
        self.P = np.array(P)
        self.Pi = self.perm()

        # Create an column index map and view to Q
        self.Qmap = dict([(j, np.where(P == j)[0]) for j in set(P)])
        self.Qview = dict([(j, self.Q[:, self.Qmap[j]])
                           for j in self.Qmap.keys()])

        # Compute Cholesky transforms
        self.Ws = [cholesky_transform(K=Ks[j], G=Gs[j][:, :len(Acts[j])], act=Acts[j])
                   if len(Acts[j]) else None for j in range(k)]

        # Fit regularization path
        self._fit_path(y)
        return

    def _fit_path(self, y):
        """ Compute the group LARS path. """
        assert self.Q is not None
        y = y.reshape((len(y), 1))
        pairs = zip(self.korder, self.korder[1:])

        # Order of columns is consistent with order in Q
        path = np.zeros((len(self.korder), self.Q.shape[1]))
        inxs = np.zeros((self.Q.shape[1],), dtype=bool)
        r = y.ravel()
        mu = 0

        # Compute steps; active columns grow incrementally
        for i, (j1, j2) in enumerate(pairs):
            Q1 = self.Qview[j1]
            Q2 = self.Qview[j2]
            c1 = norm(Q1.T.dot(r))**2 * self.f(Q1.shape[1])
            c2 = norm(Q2.T.dot(r))**2 * self.f(Q2.shape[1])
            assert c2 <= c1
            alpha = 1 - np.sqrt(c2 / c1)
            inxs[self.Qmap[j1]] = True
            path[i, inxs] = alpha * self.Q[:, inxs].T.dot(r).ravel()
            r = r - self.Q.dot(path[i])
            mu = mu + self.Q.dot(path[i])

        # Jump to least-squares solution
        path[-1] = self.Q.T.dot(r).ravel()
        r = r - self.Q.dot(path[-1])
        mu = mu + self.Q.dot(path[-1])
        assert norm(self.Q.T.dot(r)) < 1e-3
        self.path = np.cumsum(path, axis=0)
        self.mu = mu
        return

    def perm(self):
        """ Return a permutation of matrices stacked versus inferred in Q. """
        A = np.arange(len(self.Acts))
        T = A.reshape((len(A), 1)) == self.P
        Pi = np.zeros((len(self.P),), dtype=int)
        Pi[np.nonzero(T)[1]] = np.arange(len(self.P))
        return Pi

    def transform(self, Xs):
        """ Map samples in Xs to the Q-space. """
        assert self.Ks is not None
        KaW = np.hstack([K(X, K.data[a]).dot(W)
                        for j, (K, X, W, a) in enumerate(zip(self.Ks, Xs, self.Ws, self.Acts))
                        if len(a)])
        return KaW[:, self.Pi].dot(self.Ri)

    def predict(self, Xs):
        """ Predict values for Xs for the points in the active sets. """
        assert self.path is not None
        return self.transform(Xs).dot(self.path[-1]).ravel()

    def predict_path(self, Xs):
        """ Predict values for Xs for the points in the active sets across the whole regularization path. """
        assert self.path is not None
        return self.transform(Xs).dot(self.path.T)

    def predict_path_extend(self, Xs):
        """ Predict extended path as if each column was added at a time. """
        assert self.path is not None
        pth = self.transform(Xs).dot(self.path.T)
        n = Xs[0].shape[0]
        A = np.zeros((n, self.Q.shape[1]))
        curr = 0
        for ki, k in enumerate(self.korder):
            kj = self.Qview[k].shape[1]
            for i in range(kj):
                A[:, curr + i] = pth[:, ki]
            curr += kj
        return A

    def predict_path_ls(self, Xs):
        """ Predict extended path as if each column was added at a time,
            use least-squares estimate. """
        assert self.path is not None
        A = self.transform(Xs)
        pth = np.tri(self.rank, self.rank) * self.path[-1]
        return A.dot(pth.T)


# Unit tests
def test_transform():
    """ Validity of predictive transform (idpt. of penalty). """
    noise = 0.04
    n = 100
    X = np.linspace(-10, 10, n).reshape((n, 1))
    Ks = [
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 1.0}),  # short
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 0.1}),  # long
    ]
    Kt = 0.5 + Ks[0][:, :] + 0.5 * Ks[1][:, :]
    for func in (p_ri, p_const):
        for i in range(100):
            f = mvn.rvs(mean=np.zeros(n, ), cov=Kt).reshape((n, 1))
            y = mvn.rvs(mean=f.ravel(), cov=noise * np.eye(n)).reshape((n, 1))
            model = LarsMKL(rank=5, delta=5, f=func)
            model.fit(Ks, y)
            Qt = model.transform([X, X])
            Q = model.Q
            assert norm(Q - Qt) < 1e-8


def test_path_consistency():
    """ Assert that the cost decreases in accordance with the penalty function. """
    noise = 1.0
    n = 100
    X = np.linspace(-10, 10, n).reshape((n, 1))
    Ks = [
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 1.0}),    # short
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 0.1}),    # long
        ]
    Kt = 0.7 * Ks[0][:, :] + 0.3 * Ks[1][:, :]
    f = mvn.rvs(mean=np.zeros(n,), cov=Kt).reshape((n, 1))
    y = mvn.rvs(mean=f.ravel(), cov=noise * np.eye(n)).reshape((n, 1))
    for func in (p_ri, p_const):
        model = LarsMKL(rank=5, delta=5, f=func)
        model.fit(Ks, y)
        ypath = model.predict_path([X, X])
        rpath = np.hstack([y, y - ypath])
        for ri, r in enumerate(rpath.T):
            costs = [norm(model.Qview[j].T.dot(r))**2 * func(model.Qview[j].shape[1])
                     for j in model.korder]
            assert (ri == 0) or norm(costs[:ri] - costs[0]) < 1e-5


def test_ls_path_consistency():
    """ Assert that the cost decreases in accordance with the penalty function. """
    noise = 1.0
    n = 100
    X = np.linspace(-10, 10, n).reshape((n, 1))
    Ks = [
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 1.0}),  # short
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 0.1}),  # long
    ]
    Kt = 0.7 * Ks[0][:, :] + 0.3 * Ks[1][:, :]
    f = mvn.rvs(mean=np.zeros(n, ), cov=Kt).reshape((n, 1))
    y = mvn.rvs(mean=f.ravel(), cov=noise * np.eye(n)).reshape((n, 1))
    for func in (p_ri, p_const):
        model = LarsMKL(rank=5, delta=5, f=func)
        model.fit(Ks, y)
        ypath = model.predict_path_ls([X, X])
        rpath = y - ypath
        for ri, r in enumerate(rpath.T):
            assert norm(model.Q[:, :ri+1].T.dot(r)) < 1e-5


# Time test
def test_out_prediction():
    """ Compare running times. """
    np.random.seed(42)
    noise = 1.0
    n = 100
    X = np.linspace(-10, 10, n).reshape((n, 1))
    Ks = [
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 1.0}),    # short
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 0.1}),    # long
        ]
    Kt = 0.7 * Ks[0][:, :] + 0.3 * Ks[1][:, :]
    f = mvn.rvs(mean=np.zeros(n,), cov=Kt).reshape((n, 1))
    y = mvn.rvs(mean=f.ravel(), cov=noise * np.eye(n)).reshape((n, 1))
    Xt = np.linspace(-20, 20, 2*n+1).reshape((2*n+1, 1))
    model = LarsMKL(rank=10, delta=10, f=p_const)
    model.fit(Ks, y)
    yp = model.predict([Xt] * len(Ks))
    assert norm(yp[:10]) < 1e-5
    assert norm(yp[-10:]) < 1e-5


# Time test
def test_time():
    """ Compare running times. """
    np.random.seed(42)
    sigma2 = 10
    noise = 0.3
    n_range = np.linspace(100, 1000, 10, dtype=int)
    gamma_range = np.logspace(-2, 2, 10)
    rank = 30
    delta = 10
    d = 100
    results = []
    for n in n_range:
        X = sigma2 * np.random.randn(n, d)
        Ks = [Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": g}) for g in gamma_range]
        Kt = 0.7 * Ks[0][:, :] + 0.3 * Ks[1][:, :]
        f = mvn.rvs(mean=np.zeros(n,), cov=Kt).reshape((n, 1))
        y = mvn.rvs(mean=f.ravel(), cov=noise * np.eye(n)).reshape((n, 1))
        model = LarsMKL(rank=rank, delta=delta, f=p_const)
        t1 = time()
        model.fit(Ks, y)
        results.append(time() - t1)

    print("\nLARS MKL times: (kernels=%d, rank=%d)" % (len(gamma_range), rank))
    print("-------------------------------------")
    for n, t in zip(n_range, results):
        print("n=%d\tt=%.2f (s)" % (n, t))


# Plots
def plot_convergence():
    """ How fast the least-squares solution is reached? """
    noise = 0.3
    n = 300
    X = np.linspace(-10, 10, n).reshape((n, 1))
    gamma_range = np.logspace(-2, 2, 5)
    Ks = [Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": g}) for g in gamma_range]
    Kt = 0.7 * Ks[0][:, :] + 0.3 * Ks[1][:, :]
    f = mvn.rvs(mean=np.zeros(n,), cov=Kt).reshape((n, 1))
    y = mvn.rvs(mean=f.ravel(), cov=noise * np.eye(n)).reshape((n, 1))
    results = dict()
    for label, func in zip(("rich", "unscaled", "scaled"),
                           (p_ri, p_const, p_sc)):
        model = LarsMKL(rank=10, delta=5, f=func)
        model.fit(Ks, y)
        ypath = model.predict_path([X] * len(Ks))
        rpath = np.hstack([y, y - ypath])
        results[label] = norm(rpath, axis=0)

    plt.figure()
    for label in results.keys():
        plt.plot(results[label], "-", color=colors[label], label=label)
    plt.legend()
    plt.ylabel("$\|y - \mu(k)\|$")
    plt.xlabel("Number of groups (kernels)")
    plt.grid()
    plt.show()


def plot_progression(P, pmax=None, pmin=None):
    """ Plot progression of selection of kernels, given path P. """
    pmax = P.max() if pmax is None else pmax
    pmin = P.min() if pmin is None else pmin
    height = pmax - pmin + 1
    C = np.zeros((height, len(P)))
    C[P, np.arange(len(P))] = 1
    D = np.cumsum(C, axis=1)
    E = D / (1 + D.max()) + np.tile(np.arange(0, height).reshape(height, 1), len(P))
    plt.figure()
    plt.plot(E.T, "-")
    plt.plot(E[P, np.arange(len(P))], "k.")
    plt.grid()
    plt.xlabel("Step")
    plt.ylabel("Kernel")


def profiling():
    """ Profiling function. """
    np.random.seed(42)
    noise = 1.0
    n = 1000
    X = np.linspace(-10, 10, n).reshape((n, 1))
    Ks = [
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 1.0}),  # short
        Kinterface(data=X, kernel=exponential_kernel, kernel_args={"gamma": 0.1}),  # long
    ]
    Kt = 0.7 * Ks[0][:, :] + 0.3 * Ks[1][:, :]
    f = mvn.rvs(mean=np.zeros(n, ), cov=Kt).reshape((n, 1))
    y = mvn.rvs(mean=f.ravel(), cov=noise * np.eye(n)).reshape((n, 1))
    model = LarsMKL(rank=10, delta=10, f=p_const)

    import cProfile
    pr = cProfile.Profile()
    pr.enable()
    model.fit(Ks, y)
    pr.disable()
    pr.print_stats(sort="cumtime")
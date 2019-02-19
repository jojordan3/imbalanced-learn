import numbers
from copy import deepcopy

import numpy as np

from sklearn.base import clone
from sklearn.ensemble import AdaBoostClassifier
from sklearn.ensemble.base import _set_random_states
from sklearn.tree import DecisionTreeClassifier
from sklearn.utils import safe_indexing
from sklearn.externals.joblib import Parallel, delayed
from sklearn.ensemble.forest import BaseForest
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
from sklearn.tree.tree import BaseDecisionTree
from sklearn.tree._tree import DTYPE

from ..under_sampling.base import BaseUnderSampler
from ..under_sampling import RandomUnderSampler
from ..pipeline import make_pipeline
from ..utils import Substitution
from ..utils._docstring import _random_state_docstring


@Substitution(
    sampling_strategy=BaseUnderSampler._sampling_strategy_docstring,
    random_state=_random_state_docstring)
class RUSBoostClassifier(AdaBoostClassifier):
    """Random under-sampling integrating in the learning of an AdaBoost
    classifier.

    During learning, the problem of class balancing is alleviated by random
    under-sampling the sample at each iteration of the boosting algorithm.

    Read more in the :ref:`User Guide <boosting>`.

    Parameters
    ----------
    base_estimator : object, optional (default=DecisionTreeClassifier)
        The base estimator from which the boosted ensemble is built.
        Support for sample weighting is required, as well as proper `classes_`
        and `n_classes_` attributes.

    n_estimators : integer, optional (default=50)
        The maximum number of estimators at which boosting is terminated.
        In case of perfect fit, the learning procedure is stopped early.

    learning_rate : float, optional (default=1.)
        Learning rate shrinks the contribution of each classifier by
        ``learning_rate``. There is a trade-off between ``learning_rate`` and
        ``n_estimators``.

    algorithm : {{'SAMME', 'SAMME.R'}}, optional (default='SAMME.R')
        If 'SAMME.R' then use the SAMME.R real boosting algorithm.
        ``base_estimator`` must support calculation of class probabilities.
        If 'SAMME' then use the SAMME discrete boosting algorithm.
        The SAMME.R algorithm typically converges faster than SAMME,
        achieving a lower test error with fewer boosting iterations.

    {sampling_strategy}

    replacement : bool, optional (default=False)
        Whether or not to sample randomly with replacement or not.

    {random_state}

    verbose : int, optional (default=0)
        Controls the verbosity of the building process.

    Attributes
    ----------
    estimators_ : list of classifiers
        The collection of fitted sub-estimators.

    samplers_ : list of RandomUnderSampler
        The collection of fitted samplers.

    pipelines_ : list of Pipeline.
        The collection of fitted pipelines (samplers + trees).

    classes_ : ndarray, shape (n_classes,)
        The classes labels.

    n_classes_ : int
        The number of classes.

    estimator_weights_ : ndarray, shape (n_estimator,)
        Weights for each estimator in the boosted ensemble.

    estimator_errors_ : ndarray, shape (n_estimator,)
        Classification error for each estimator in the boosted
        ensemble.

    feature_importances_ : ndarray, shape (n_features,)
        The feature importances if supported by the ``base_estimator``.

    See also
    --------
    BalancedBaggingClassifier, BalancedRandomForestClassifier,
    EasyEnsembleClassifier

    References
    ----------
    .. [1] Seiffert, C., Khoshgoftaar, T. M., Van Hulse, J., & Napolitano, A.
       "RUSBoost: A hybrid approach to alleviating class imbalance." IEEE
       Transactions on Systems, Man, and Cybernetics-Part A: Systems and Humans
       40.1 (2010): 185-197.

    Examples
    --------
    >>> from imblearn.ensemble import RUSBoostClassifier
    >>> from sklearn.datasets import make_classification
    >>>
    >>> X, y = make_classification(n_samples=1000, n_classes=3,
    ...                            n_informative=4, weights=[0.2, 0.3, 0.5],
    ...                            random_state=0)
    >>> clf = RUSBoostClassifier(random_state=0)
    >>> clf.fit(X, y)  # doctest: +ELLIPSIS
    RUSBoostClassifier(...)
    >>> clf.predict(X)  # doctest: +ELLIPSIS
    array([...])
    """

    def __init__(self, base_estimator=None, n_estimators=50, learning_rate=1.,
                 algorithm='SAMME.R', sampling_strategy='auto',
                 replacement=False, random_state=None, verbose=0):
        super(RUSBoostClassifier, self).__init__(
            base_estimator=base_estimator,
            n_estimators=n_estimators,
            learning_rate=learning_rate,
            algorithm=algorithm,
            random_state=random_state)
        self.sampling_strategy = sampling_strategy
        self.replacement = replacement
        self.verbose = verbose

    def fit(self, X, y, sample_weight=None):
        """Build a boosted classifier from the training set (X, y).

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            The training input samples. Sparse matrix can be CSC, CSR, COO,
            DOK, or LIL. DOK and LIL are converted to CSR.

        y : array-like, shape (n_samples,)
            The target values (class labels).

        sample_weight : array-like, shape (n_samples,), optional
            Sample weights. If None, the sample weights are initialized to
            ``1 / n_samples``.

        Returns
        -------
        self : object
            Returns self.

        """
        # Check parameters
        if self.learning_rate <= 0:
            raise ValueError("learning_rate must be greater than zero")

        if (self.base_estimator is None or
                isinstance(self.base_estimator, (BaseDecisionTree,
                                                 BaseForest))):
            dtype = DTYPE
            accept_sparse = 'csc'
        else:
            dtype = None
            accept_sparse = ['csr', 'csc']

        X, y = check_X_y(X, y, accept_sparse=accept_sparse, dtype=dtype,
                         y_numeric=is_regressor(self))

        if sample_weight is None:
            # Initialize weights to 1 / n_samples
            sample_weight = np.empty(X.shape[0], dtype=np.float64)
            sample_weight[:] = 1. / X.shape[0]
        else:
            sample_weight = check_array(sample_weight, ensure_2d=False)
            # Normalize existing weights
            sample_weight = sample_weight / sample_weight.sum(dtype=np.float64)

            # Check that the sample weights sum is positive
            if sample_weight.sum() <= 0:
                raise ValueError(
                    "Attempting to fit with a non-positive "
                    "weighted number of samples.")

        # Check parameters
        self._validate_estimator()

        # Clear any previous fit results
        self.samplers_ = []
        self.pipelines_ = []
        self.estimators_ = []
        self.estimator_weights_ = np.zeros(self.n_estimators, dtype=np.float64)
        self.estimator_errors_ = np.ones(self.n_estimators, dtype=np.float64)

        random_state = check_random_state(self.random_state)

        for iboost in range(self.n_estimators):
            if verbose > 0:
                print(f'Fitting {iboost + 1} out of {self.n_estimators}\
                      estimators')
            # Boosting step
            sample_weight, estimator_weight, estimator_error = super(
                RUSBoostClassifier, self)._boost(iboost, X, y, sample_weight,
                                                 random_state)

            if verbose > 1:
                print('\tEstimator error: {estimator_error:.4f}\
                       ------------------------------')

            # Early termination
            if sample_weight is None:
                break

            self.estimator_weights_[iboost] = estimator_weight
            self.estimator_errors_[iboost] = estimator_error

            # Stop if error is zero
            if estimator_error == 0:
                if verbose > 0:
                    print('-------------DONE-------------')
                break

            sample_weight_sum = np.sum(sample_weight)

            # Stop if the sum of sample weights has become non-positive
            if sample_weight_sum <= 0:
                if verbose > 0:
                    print('-------------DONE-------------\
                          MSG: sum of sample weights has become non-positive')
                break

            if iboost < self.n_estimators - 1:
                # Normalize
                sample_weight /= sample_weight_sum

        return self

    def _validate_estimator(self, default=DecisionTreeClassifier()):
        """Check the estimator and the n_estimator attribute, set the
        `base_estimator_` attribute."""
        if not isinstance(self.n_estimators, (numbers.Integral, np.integer)):
            raise ValueError("n_estimators must be an integer, "
                             "got {0}.".format(type(self.n_estimators)))

        if self.n_estimators <= 0:
            raise ValueError("n_estimators must be greater than zero, "
                             "got {0}.".format(self.n_estimators))

        if self.base_estimator is not None:
            self.base_estimator_ = clone(self.base_estimator)
        else:
            self.base_estimator_ = clone(default)

        self.base_sampler_ = RandomUnderSampler(
            sampling_strategy=self.sampling_strategy,
            replacement=self.replacement)

    def _make_sampler_estimator(self, append=True, random_state=None):
        """Make and configure a copy of the `base_estimator_` attribute.
        Warning: This method should be used to properly instantiate new
        sub-estimators.
        """
        estimator = clone(self.base_estimator_)
        estimator.set_params(**dict((p, getattr(self, p))
                                    for p in self.estimator_params))
        sampler = clone(self.base_sampler_)

        if random_state is not None:
            _set_random_states(estimator, random_state)
            _set_random_states(sampler, random_state)

        if append:
            self.estimators_.append(estimator)
            self.samplers_.append(sampler)
            self.pipelines_.append(make_pipeline(deepcopy(sampler),
                                                 deepcopy(estimator)))

        return estimator, sampler

    def _boost_real(self, iboost, X, y, sample_weight, random_state):
        """Implement a single boost using the SAMME.R real algorithm."""
        estimator, sampler = self._make_sampler_estimator(
            random_state=random_state)

        X_res, y_res = sampler.fit_resample(X, y)
        sample_weight_res = safe_indexing(sample_weight,
                                          sampler.sample_indices_)
        estimator.fit(X_res, y_res, sample_weight=sample_weight_res)

        y_predict_proba = estimator.predict_proba(X)

        if iboost == 0:
            self.classes_ = getattr(estimator, 'classes_', None)
            self.n_classes_ = len(self.classes_)

        y_predict = self.classes_.take(np.argmax(y_predict_proba, axis=1),
                                       axis=0)

        # Instances incorrectly classified
        incorrect = y_predict != y

        # Error fraction
        estimator_error = np.mean(
            np.average(incorrect, weights=sample_weight, axis=0))

        # Stop if classification is perfect
        if estimator_error <= 0:
            return sample_weight, 1., 0.

        # Construct y coding as described in Zhu et al [2]:
        #
        #    y_k = 1 if c == k else -1 / (K - 1)
        #
        # where K == n_classes_ and c, k in [0, K) are indices along the second
        # axis of the y coding with c being the index corresponding to the true
        # class label.
        n_classes = self.n_classes_
        classes = self.classes_
        y_codes = np.array([-1. / (n_classes - 1), 1.])
        y_coding = y_codes.take(classes == y[:, np.newaxis])

        # Displace zero probabilities so the log is defined.
        # Also fix negative elements which may occur with
        # negative sample weights.
        proba = y_predict_proba  # alias for readability
        np.clip(proba, np.finfo(proba.dtype).eps, None, out=proba)

        # Boost weight using multi-class AdaBoost SAMME.R alg
        estimator_weight = (-1. * self.learning_rate
                            * ((n_classes - 1.) / n_classes)
                            * (y_coding * np.log(y_predict_proba)).sum(axis=1))

        # Only boost the weights if it will fit again
        if not iboost == self.n_estimators - 1:
            # Only boost positive weights
            sample_weight *= np.exp(estimator_weight *
                                    ((sample_weight > 0) |
                                     (estimator_weight < 0)))

        return sample_weight, 1., estimator_error

    def _boost_discrete(self, iboost, X, y, sample_weight, random_state):
        """Implement a single boost using the SAMME discrete algorithm."""
        estimator, sampler = self._make_sampler_estimator(
            random_state=random_state)

        X_res, y_res = sampler.fit_resample(X, y)
        sample_weight_res = safe_indexing(sample_weight,
                                          sampler.sample_indices_)
        estimator.fit(X_res, y_res, sample_weight=sample_weight_res)

        y_predict = estimator.predict(X)

        if iboost == 0:
            self.classes_ = getattr(estimator, 'classes_', None)
            self.n_classes_ = len(self.classes_)

        # Instances incorrectly classified
        incorrect = y_predict != y

        # Error fraction
        estimator_error = np.mean(
            np.average(incorrect, weights=sample_weight, axis=0))

        # Stop if classification is perfect
        if estimator_error <= 0:
            return sample_weight, 1., 0.

        n_classes = self.n_classes_

        # Stop if the error is at least as bad as random guessing
        if estimator_error >= 1. - (1. / n_classes):
            self.estimators_.pop(-1)
            self.samplers_.pop(-1)
            self.pipelines_.pop(-1)
            if len(self.estimators_) == 0:
                raise ValueError('BaseClassifier in AdaBoostClassifier '
                                 'ensemble is worse than random, ensemble '
                                 'can not be fit.')
            return None, None, None

        # Boost weight using multi-class AdaBoost SAMME alg
        estimator_weight = self.learning_rate * (
            np.log((1. - estimator_error) / estimator_error) +
            np.log(n_classes - 1.))

        # Only boost the weights if I will fit again
        if not iboost == self.n_estimators - 1:
            # Only boost positive weights
            sample_weight *= np.exp(estimator_weight * incorrect *
                                    ((sample_weight > 0) |
                                     (estimator_weight < 0)))

        return sample_weight, estimator_weight, estimator_error

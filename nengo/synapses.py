import collections
import warnings

import numpy as np

from nengo.base import Process
from nengo.exceptions import ValidationError
from nengo.params import (BoolParam, NdarrayParam, NumberParam, Parameter,
                          Unconfigurable)
from nengo.utils.compat import is_number
from nengo.utils.filter_design import cont2discrete, tf2ss
from nengo.utils.numpy import as_shape


class Synapse(Process):
    """Abstract base class for synapse models.

    Conceptually, a synapse model emulates a biological synapse, taking in
    input in the form of released neurotransmitter and opening ion channels
    to allow more or less current to flow into the neuron.

    In Nengo, the implementation of a synapse is as a specific case of a
    `.Process` in which the input and output shapes are the same.
    The input is the current across the synapse, and the output is the current
    that will be induced in the postsynaptic neuron.

    Synapses also contain the `.Synapse.filt` and `.Synapse.filtfilt` methods,
    which make it easy to use Nengo's synapse models outside of Nengo
    simulations.

    Parameters
    ----------
    default_size_in : int, optional (Default: 1)
        The size_in used if not specified.
    default_size_out : int (Default: None)
        The size_out used if not specified.
        If None, will be the same as default_size_in.
    default_dt : float (Default: 0.001 (1 millisecond))
        The simulation timestep used if not specified.
    seed : int, optional (Default: None)
        Random number seed. Ensures random factors will be the same each run.

    Attributes
    ----------
    default_dt : float (Default: 0.001 (1 millisecond))
        The simulation timestep used if not specified.
    default_size_in : int (Default: 0)
        The size_in used if not specified.
    default_size_out : int (Default: 1)
        The size_out used if not specified.
    seed : int, optional (Default: None)
        Random number seed. Ensures random factors will be the same each run.
    """

    def __init__(self, default_size_in=1, default_size_out=None,
                 default_dt=0.001, seed=None):
        if default_size_out is None:
            default_size_out = default_size_in
        super(Synapse, self).__init__(default_size_in=default_size_in,
                                      default_size_out=default_size_out,
                                      default_dt=default_dt,
                                      seed=seed)

    def filt(self, x, dt=None, axis=0, y0=None, copy=True, filtfilt=False):
        """Filter ``x`` with this synapse model.

        Parameters
        ----------
        x : array_like
            The signal to filter.
        dt : float, optional (Default: None)
            The timestep of the input signal.
            If None, ``default_dt`` will be used.
        axis : int, optional (Default: 0)
            The axis along which to filter.
        y0 : array_like, optional (Default: None)
            The starting state of the filter output. If None, the initial
            value of the input signal along the axis filtered will be used.
        copy : bool, optional (Default: True)
            Whether to copy the input data, or simply work in-place.
        filtfilt : bool, optional (Default: False)
            If True, runs the process forward then backward on the signal,
            for zero-phase filtering (like Matlab's ``filtfilt``).
        """
        # This function is very similar to `Process.apply`, but allows for
        # a) filtering along any axis, and b) zero-phase filtering (filtfilt).
        dt = self.default_dt if dt is None else dt
        filtered = np.array(x, copy=copy)
        filt_view = np.rollaxis(filtered, axis=axis)  # rolled view on filtered

        if y0 is None:
            y0 = filt_view[0]

        shape_in = shape_out = as_shape(filt_view[0].shape, min_dim=1)
        step = self.make_step(
            shape_in, shape_out, dt, None, y0=y0, dtype=filtered.dtype)

        for i, signal_in in enumerate(filt_view):
            filt_view[i] = step(i * dt, signal_in)

        if filtfilt:  # Flip the filt_view and filter again
            n = len(filt_view) - 1
            filt_view = filt_view[::-1]
            for i, signal_in in enumerate(filt_view):
                filt_view[i] = step((n - i) * dt, signal_in)

        return filtered

    def filtfilt(self, x, **kwargs):
        """Zero-phase filtering of ``x`` using this filter.

        Equivalent to `filt(x, filtfilt=True, **kwargs) <.Synapse.filt>`.
        """
        return self.filt(x, filtfilt=True, **kwargs)

    def make_step(self, shape_in, shape_out, dt, rng, state=None,
                  y0=None, dtype=np.float64):
        """Create function that advances the synapse forward one time step.

        At a minimum, Synapse subclasses must implement this method.
        That implementation should return a callable that will perform
        the synaptic filtering operation.

        Parameters
        ----------
        shape_in : tuple
            Shape of the input signal to be filtered.
        shape_out : tuple
            Shape of the output filtered signal.
        dt : float
            The timestep of the simulation.
        rng : `numpy.random.RandomState`
            Random number generator.
        y0 : array_like, optional (Default: None)
            The starting state of the filter output. If None, each dimension
            of the state will start at zero.
        dtype : `numpy.dtype` (Default: np.float64)
            Type of data used by the synapse model. This is important for
            ensuring that certain synapses avoid or force integer division.
        """
        raise NotImplementedError("Synapses should implement make_step.")


class LinearFilter(Synapse):
    """General linear time-invariant (LTI) system synapse.

    This class can be used to implement any linear filter, given the
    filter's transfer function. [1]_

    Parameters
    ----------
    num : array_like
        Numerator coefficients of transfer function.
    den : array_like
        Denominator coefficients of transfer function.
    analog : boolean, optional (Default: True)
        Whether the synapse coefficients are analog (i.e. continuous-time),
        or discrete. Analog coefficients will be converted to discrete for
        simulation using the simulator ``dt``.

    Attributes
    ----------
    analog : boolean
        Whether the synapse coefficients are analog (i.e. continuous-time),
        or discrete. Analog coefficients will be converted to discrete for
        simulation using the simulator ``dt``.
    den : ndarray
        Denominator coefficients of transfer function.
    num : ndarray
        Numerator coefficients of transfer function.

    References
    ----------
    .. [1] https://en.wikipedia.org/wiki/Filter_%28signal_processing%29
    """

    num = NdarrayParam('num', shape='*')
    den = NdarrayParam('den', shape='*')
    analog = BoolParam('analog')

    def __init__(self, num, den, analog=True, **kwargs):
        super(LinearFilter, self).__init__(**kwargs)
        self.num = num
        self.den = den
        self.analog = analog

    def __repr__(self):
        return "%s(%s, %s, analog=%r)" % (
            type(self).__name__, self.num, self.den, self.analog)

    def combine(self, obj):
        """Combine in series with another LinearFilter."""
        if not isinstance(obj, LinearFilter):
            raise ValidationError(
                "Can only combine with other LinearFilters", attr='obj')
        if self.analog != obj.analog:
            raise ValidationError(
                "Cannot combine analog and digital filters", attr='obj')
        num = np.polymul(self.num, obj.num)
        den = np.polymul(self.den, obj.den)
        return LinearFilter(num, den,
                            analog=self.analog,
                            default_size_in=self.default_size_in,
                            default_size_out=self.default_size_out,
                            default_dt=self.default_dt,
                            seed=self.seed)

    def evaluate(self, frequencies):
        """Evaluate the transfer function at the given frequencies.

        Examples
        --------

        Using the ``evaluate`` function to make a Bode plot::

            synapse = nengo.synapses.LinearFilter([1], [0.02, 1])
            f = numpy.logspace(-1, 3, 100)
            y = synapse.evaluate(f)
            plt.subplot(211); plt.semilogx(f, 20*np.log10(np.abs(y)))
            plt.xlabel('frequency [Hz]'); plt.ylabel('magnitude [dB]')
            plt.subplot(212); plt.semilogx(f, np.angle(y))
            plt.xlabel('frequency [Hz]'); plt.ylabel('phase [radians]')
        """
        frequencies = 2.j*np.pi*frequencies
        w = frequencies if self.analog else np.exp(frequencies)
        y = np.polyval(self.num, w) / np.polyval(self.den, w)
        return y

    def _get_ss(self, dt, method='zoh'):
        A, B, C, D = tf2ss(self.num, self.den)
        D = D[None, :] if D.ndim == 1 else D
        if A.size == 0:
            return A, B, C, D  # only D is non-empty

        if self.analog:
            A, B, C, D, _ = cont2discrete((A, B, C, D), dt, method=method)

        # Normalize so output is unscaled sum of states
        assert C.shape[0] == 1
        q = np.array(C[0])
        q[np.abs(q) < 1e-8] = 1
        A = q[:, None] * A * (1./q)[None, :]
        B = q[:, None] * B
        C = C * (1./q)[None, :]

        # Remove state if A is small
        if (np.abs(A) < 1e-16).all():
            D = D + np.dot(C, B)
            A = np.array([], float)
            B = np.array([], float)
            C = np.array([], float)

        return A, B, C, D

    def allocate(self, shape_in, shape_out, dt,
                 y0=None, dtype=np.float64, method='zoh'):
        assert shape_in == shape_out

        A, B, C, D = self._get_ss(dt, method=method)
        X = np.zeros((len(A),) + shape_out, dtype=dtype)

        if y0 is not None and len(X) > 0:
            y0 = np.asarray(y0)
            if y0.ndim == 0:
                y0 = y0.reshape((1,))
            y0 = y0[None, ...]
            X[:] = np.linalg.solve(np.eye(A.shape[0]) - A, np.dot(B, y0))

        return dict(X=X)

    def make_step(self, shape_in, shape_out, dt, rng, state=None,
                  y0=None, dtype=np.float64, method='zoh'):
        """Returns a `.Step` instance that implements the linear filter."""
        assert shape_in == shape_out
        if state is None:
            state = self.allocate(
                shape_in, shape_out, dt, dtype=dtype, y0=y0, method=method)

        A, B, C, D = self._get_ss(dt, method=method)
        X = state['X']
        if LinearFilter.NoA.check(A, B, C, D, X):
            return LinearFilter.NoA(A, B, C, D, X)
        elif LinearFilter.OneA.check(A, B, C, D, X):
            return LinearFilter.OneA(A, B, C, D, X)
        elif LinearFilter.OneC.check(A, B, C, D, X):
            return LinearFilter.OneC(A, B, C, D, X)
        elif LinearFilter.General.check(A, B, C, D, X):
            return LinearFilter.General(A, B, C, D, X)
        else:
            raise ValidationError("No suitable step function found",
                                  attr='make_step', obj=self)

    class Step(object):
        """Abstract base class for LTI filtering step functions."""
        def __init__(self, A, B, C, D, X):
            if not self.check(A, B, C, D, X):
                raise ValidationError(
                    "Matrices do not meet the requirements for this Step",
                    attr='matrices', obj=self)
            self.A = A
            self.B = B
            self.C = C
            self.D = D
            self.noD = (D == 0).all()
            self.X = X

        def __call__(self, t, signal):
            raise NotImplementedError("Step object must implement __call__")

        @classmethod
        def check(cls, A, B, C, D, X):
            return B.size == 0 or B.shape[1] == 1

    class NoA(Step):
        """An LTI step function for transfer functions with no denominator.

        This step function should be much faster than the equivalent general
        step function.
        """
        def __init__(self, A, B, C, D, X):
            super(LinearFilter.NoA, self).__init__(A, B, C, D, X)
            self.d = D.item()

        def __call__(self, t, signal):
            return self.d * signal

        @classmethod
        def check(cls, A, B, C, D, X):
            return super(LinearFilter.NoA, cls).check(A, B, C, D, X) and (
                A.size == 0 and B.size == 0 and C.size == 0 and D.size == 1)

    class OneA(Step):
        """An LTI step function for transfer functions with one num and den.

        This step function should be much faster than the equivalent general
        step function.
        """
        def __init__(self, A, B, C, D, X):
            super(LinearFilter.OneA, self).__init__(A, B, C, D, X)
            self.a = A[0, 0]
            self.b = B[0, 0]
            self.c = C[0, 0]
            self.d = D[0, 0]

        def __call__(self, t, signal):
            self.X[...] *= self.a
            self.X[...] += self.b * signal
            Y = self.X if self.c == 1 else self.c*self.X
            return Y if self.noD else (Y + self.d*signal)

        @classmethod
        def check(cls, A, B, C, D, X):
            return super(LinearFilter.OneA, cls).check(A, B, C, D, X) and (
                A.size == 1 and B.size == 1 and C.size == 1 and D.size == 1)

    class OneC(Step):
        """An LTI step function for any given system."""
        def __init__(self, A, B, C, D, X):
            super(LinearFilter.OneC, self).__init__(A, B, C, D, X)
            self.Ci = C[0].nonzero()[0][0]

        def __call__(self, t, signal):
            signal = signal[None, ...]
            self.X[...] = np.dot(self.A, self.X) + np.dot(self.B, signal)
            Y = self.X[self.Ci]
            return Y if self.noD else (Y + np.dot(self.D, signal))

        @classmethod
        def check(cls, A, B, C, D, X):
            return super(LinearFilter.OneC, cls).check(A, B, C, D, X) and (
                C.shape[0] == 1 and len(C[0].nonzero()[0]) == 1)

    class General(Step):
        """An LTI step function for any given system."""
        def __call__(self, t, signal):
            signal = signal[None, ...]
            self.X[...] = np.dot(self.A, self.X) + np.dot(self.B, signal)
            Y = np.dot(self.C, self.X)
            return Y if self.noD else (Y + np.dot(self.D, signal))


class Lowpass(LinearFilter):
    """Standard first-order lowpass filter synapse.

    The impulse-response function is given by::

        f(t) = (t / tau) * exp(-t / tau)

    Parameters
    ----------
    tau : float
        The time constant of the filter in seconds.

    Attributes
    ----------
    tau : float
        The time constant of the filter in seconds.
    """
    tau = NumberParam('tau', low=0)

    def __init__(self, tau, **kwargs):
        super(Lowpass, self).__init__([1], [tau, 1], **kwargs)
        self.tau = tau

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.tau)


class Alpha(LinearFilter):
    """Alpha-function filter synapse.

    The impulse-response function is given by::

        alpha(t) = (t / tau**2) * exp(-t / tau)

    and was found by [1]_ to be a good basic model for synapses.

    Parameters
    ----------
    tau : float
        The time constant of the filter in seconds.

    Attributes
    ----------
    tau : float
        The time constant of the filter in seconds.

    References
    ----------
    .. [1] Mainen, Z.F. and Sejnowski, T.J. (1995). Reliability of spike timing
       in neocortical neurons. Science (New York, NY), 268(5216):1503-6.
    """

    tau = NumberParam('tau', low=0)

    def __init__(self, tau, **kwargs):
        super(Alpha, self).__init__([1], [tau**2, 2*tau, 1], **kwargs)
        self.tau = tau

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.tau)


class Triangle(Synapse):
    """Triangular finite impulse response (FIR) synapse.

    This synapse has a triangular and finite impulse response. The length of
    the triangle is ``t`` seconds; thus the digital filter will have
    ``t / dt + 1`` taps.

    Parameters
    ----------
    t : float
        Length of the triangle, in seconds.

    Attributes
    ----------
    t : float
        Length of the triangle, in seconds.
    """

    t = NumberParam('t', low=0)

    def __init__(self, t, **kwargs):
        super(Triangle, self).__init__(**kwargs)
        self.t = t

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.t)

    def make_step(self, shape_in, shape_out, dt, rng, state=None,
                  y0=None, dtype=np.float64):
        """Returns a custom step function."""
        assert shape_in == shape_out

        n_taps = int(np.round(self.t / float(dt))) + 1
        num = np.arange(n_taps, 0, -1, dtype=np.float64)
        num /= num.sum()

        # Minimal multiply implementation finds the difference between
        # coefficients and subtracts a scaled signal at each time step.
        n0, ndiff = num[0].astype(dtype), num[-1].astype(dtype)
        x = collections.deque(maxlen=n_taps)

        output = np.zeros(shape_out, dtype=dtype)
        if y0 is not None:
            output[:] = y0

        def step_triangle(t, signal):
            output[...] += n0 * signal
            for xk in x:
                output[...] -= xk
            x.appendleft(ndiff * signal)
            return output

        return step_triangle


def filt(signal, synapse, dt, axis=0, x0=None, copy=True):
    """Filter ``signal`` with ``synapse``.

    .. note:: Deprecated in Nengo 2.1.0.
              Use `.Synapse.filt` method instead.
    """
    warnings.warn("Use ``synapse.filt`` instead", DeprecationWarning)
    return synapse.filt(signal, dt=dt, axis=axis, y0=x0, copy=copy)


def filtfilt(signal, synapse, dt, axis=0, x0=None, copy=True):
    """Zero-phase filtering of ``signal`` using the ``synapse`` filter.

    .. note:: Deprecated in Nengo 2.1.0.
              Use `.Synapse.filtfilt` method instead.
    """
    warnings.warn("Use ``synapse.filtfilt`` instead", DeprecationWarning)
    return synapse.filtfilt(signal, dt=dt, axis=axis, y0=x0, copy=copy)


class SynapseParam(Parameter):
    equatable = True

    def __init__(self, name,
                 default=Unconfigurable, optional=True, readonly=None):
        super(SynapseParam, self).__init__(name, default, optional, readonly)

    def coerce(self, instance, synapse):
        synapse = Lowpass(synapse) if is_number(synapse) else synapse
        self.check_type(instance, synapse, Synapse)
        return super(SynapseParam, self).coerce(instance, synapse)

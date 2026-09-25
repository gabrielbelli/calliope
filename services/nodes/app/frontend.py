# The echo canceller below is ported from Speex's MDF echo canceller
# (libspeexdsp/mdf.c in xiph/speexdsp): the two-path foreground/background
# logic with its thresholds (VAR1_UPDATE, VAR2_UPDATE, VAR_BACKTRACK), the leak
# estimate and its rates (spec_average, beta0, beta_max, MIN_LEAK), and the
# learning-rate formulas from Valin's paper as mdf.c implements them. Changed
# from the original: vectorised over microphones in numpy, floating point
# only, one reference channel, and no preprocessing (DC notch, pre-emphasis),
# which the noise suppressor's low cut stands in for. The notice below is
# mdf.c's own, and it travels with this file.
#
#   Copyright (C) 2003-2008 Jean-Marc Valin
#
#   Redistribution and use in source and binary forms, with or without
#   modification, are permitted provided that the following conditions are
#   met:
#
#   1. Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
#
#   2. Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in the
#   documentation and/or other materials provided with the distribution.
#
#   3. The name of the author may not be used to endorse or promote products
#   derived from this software without specific prior written permission.
#
#   THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
#   IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
#   OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#   DISCLAIMED. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT,
#   INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#   (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#   SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
#   HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
#   STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
#   ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#   POSSIBILITY OF SUCH DAMAGE.

"""The live audio front-end for a node's microphones, in numpy alone.

A node sends four channels at 16 kHz: its own speaker loopback, then three
microphones. `FrontEnd.process` turns those into one clean mono channel, in
three stages that run on every 16 ms block:

1. ECHO CANCELLATION, one adaptive filter per microphone against the loopback.
   The loopback is sampled by the same ADC as the microphones, so it is already
   sample-aligned and needs no delay search. The filter is a partitioned-block
   frequency-domain adaptive filter (the "MDF" of Speex), 128 ms long. Its step
   size is set per bin, per block, from an estimate of how much echo is left in
   the error (Valin, "On adjusting the learning rate in frequency domain echo
   cancellation with double-talk", 2007): when the error grows because someone
   near the node is talking, the residual echo estimate does not, and the step
   collapses on its own. A second, foreground copy of the filter is the one
   whose output is used; the adapting copy is promoted only when it is
   measurably better, and reset from the foreground when it is measurably
   worse, so a burst of double-talk that slips through cannot wreck the output.

2. BEAMFORMING across the three echo-cancelled microphones: MVDR, with the
   noise covariance tracked while nobody is talking and the talker's steering
   vector estimated as the generalised eigenvector of the speech covariance
   against the noise covariance. PER-BIN EIGENVECTORS ARE NOT USED AS THEY
   COME: between the harmonics of a voice a bin holds only noise, and its
   eigenvector points anywhere. They are fitted to a clean delay model instead,
   one delay and one broadband gain per microphone, weighted by how much speech
   each bin holds. In an
   offline demo on a real take, that fit is what made MVDR work (+12.9 dB).
   Until enough statistics exist the beam falls back to delay-and-sum, and
   before any talker has been heard, to the plain average of the microphones.
   Diagonal loading keeps the weights from amplifying the microphones' own
   noise where the array is too small to be directive.

3. NOISE SUPPRESSION on the beam: a decision-directed Wiener gain with a floor
   of -18 dB, over a noise estimate tracked by speech presence probability
   (Gerkmann and Hendriks, 2012). The echo canceller's own estimate of its
   residual is added to that noise, with a deeper floor, so what the linear
   filter could not remove is suppressed as well. Everything below 60 Hz is
   removed: DC offset and mains hum, never speech.

All three share one STFT (512-point frames, sqrt-Hann windows, 256-sample hop),
so the whole pipeline costs one analysis and one synthesis.

LATENCY. Output sample k is input sample k - 256 (16 ms). A sample can also wait
up to 255 samples for its block to fill, so the worst case is 511 samples,
32 ms, against a budget of 64 ms. The node's own 20 ms framing is upstream and
not counted.

DIRECTION. `direction` is the azimuth of the talker the beam is steered at,
from the fitted delays, ASSUMING the three microphones sit on an equilateral
triangle of side `spacing_m`, numbered counter-clockwise seen from above. 0
degrees points from the centre of the array towards the first microphone and 120
towards the second. The Korvo's microphone geometry and its orientation in a
room have not been calibrated, so this angle is relative to the board, and its
sense flips if the microphones actually run clockwise. The beamformer does not
depend on the assumption; only the reported angle does.

WHAT IT CANNOT DO. The speech/noise decision is by stationarity: a steady fan
or hum is noise, anything that comes and goes is speech. A television or a
second talker therefore counts as speech, and the beam may steer to whichever
is louder. While the node's own speaker is playing, the beam's statistics are
frozen, so the node's voice never becomes the talker or the noise; a barge-in
is heard through the beam as it was steered before playback.
"""

from __future__ import annotations

import math
import time

import numpy as np

SPEED_OF_SOUND = 343.0  # m/s, dry air at 20 C

# A block whose reference is quieter than this is not the node playing
# anything. The Korvo's idle loopback measured -89 dBFS; its microphones' idle
# floor -56 to -63 dBFS.
FAR_END_DBFS = -60.0
# Frames quieter than this at the output are never speech, whatever their
# spectral shape: below the idle floor of the Korvo's microphones.
SILENCE_DBFS = -70.0
NOISE_FLOOR_DB = -18.0  # the Wiener gain never goes below this for noise
ECHO_FLOOR_DB = -40.0   # nor below this for residual echo
LOW_CUT_HZ = 60.0

_TINY = 1e-12


def _dbfs_power(dbfs: float) -> float:
    """Mean-square value, in int16 units squared, of a signal at `dbfs`."""
    return (32768.0 * 10 ** (dbfs / 20)) ** 2


class EchoCanceller:
    """Partitioned-block frequency-domain adaptive filter, one per microphone.

    `process` takes a block of microphone samples (mics, block) and the
    reference block (block,), both float in int16 units, and returns the
    echo-cancelled microphones and the echo estimate that was taken out, both
    (mics, block). The block is the partition length: `partitions` blocks make
    the filter's reach.
    """

    MIN_LEAK = 0.005  # never trust the canceller to reach better than -23 dB

    def __init__(self, block: int, partitions: int, mics: int, rate: int):
        self.n, self.p, self.m = block, partitions, mics
        self.bins = block + 1
        # Valin's rates, scaled to the block length: how quickly the leak
        # regression may follow new evidence.
        self.spec_average = block / rate
        self.beta0 = 2.0 * block / rate
        self.beta_max = 0.5 * block / rate
        self.ss = 0.35 / partitions
        k = np.arange(block)
        self.fade_in = 0.5 - 0.5 * np.cos(np.pi * (k + 0.5) / block)
        self.fade_out = 1.0 - self.fade_in
        # Regularises the step where the reference has no energy: one LSB of
        # white noise, in the units of an unnormalised 2*block FFT.
        self.reg = 2.0 * block
        self.reset()

    def reset(self) -> None:
        n, p, m, f = self.n, self.p, self.m, self.bins
        self.x_prev = np.zeros(n)
        self.X = np.zeros((p, f), complex)       # reference spectra, newest first
        self.W = np.zeros((m, p, f), complex)    # background filter: adapts
        self.Wf = np.zeros((m, p, f), complex)   # foreground filter: is used
        self.power = np.zeros(f)
        self.Eh = np.zeros((m, f))
        self.Yh = np.zeros((m, f))
        self.Pey = np.ones(m)
        self.Pyy = np.ones(m)
        self.leak = np.ones(m)
        self.sum_adapt = 0.0
        self.adapted = False
        self.davg1 = np.zeros(m)
        self.davg2 = np.zeros(m)
        self.dvar1 = np.zeros(m)
        self.dvar2 = np.zeros(m)
        self.pad = np.zeros((2, m, 2 * n))
        self.sdd = 0.0
        self.see = 0.0
        self.active_blocks = 0

    @property
    def erle_db(self) -> float | None:
        if self.sdd <= 0:
            return None
        return 10 * math.log10((self.sdd + _TINY) / (self.see + _TINY))

    def impulse_response(self) -> np.ndarray:
        """The foreground filters in the time domain, (mics, partitions*block)."""
        w = np.fft.irfft(self.Wf, n=2 * self.n, axis=-1)[..., : self.n]
        return w.reshape(self.m, -1)

    def process(self, d: np.ndarray, x: np.ndarray, active: bool) -> tuple[np.ndarray, np.ndarray]:
        n = self.n
        X = np.fft.rfft(np.concatenate((self.x_prev, x)))
        self.x_prev = x.copy()
        self.X[1:] = self.X[:-1]
        self.X[0] = X
        Y = np.stack(((self.Wf * self.X).sum(axis=1), (self.W * self.X).sum(axis=1)))
        y = np.fft.irfft(Y, n=2 * n, axis=-1)[..., n:]
        y_fg, y_bg = y[0], y[1]
        e_fg = d - y_fg
        if not active:
            # Nothing is playing, so there is nothing to learn: adapting now
            # would only fit the reference's idle noise to the room.
            return e_fg, y_fg

        self.active_blocks += 1
        e_bg = d - y_bg
        sff = np.einsum("mn,mn->m", e_fg, e_fg)
        see = np.einsum("mn,mn->m", e_bg, e_bg)
        e_out, y_out = e_fg, y_fg

        # Two-path logic (Speex): compare the errors of the two filters over a
        # short and a longer window, relative to how different their outputs
        # are, and only act on a difference that is statistically real.
        diff = y_fg - y_bg
        dbf = 10.0 + np.einsum("mn,mn->m", diff, diff)
        delta = sff - see
        self.davg1 = 0.6 * self.davg1 + 0.4 * delta
        self.davg2 = 0.85 * self.davg2 + 0.15 * delta
        self.dvar1 = 0.36 * self.dvar1 + 0.16 * sff * dbf
        self.dvar2 = 0.7225 * self.dvar2 + 0.0225 * sff * dbf
        promote = ((delta * np.abs(delta) > sff * dbf)
                   | (self.davg1 * np.abs(self.davg1) > 0.5 * self.dvar1)
                   | (self.davg2 * np.abs(self.davg2) > 0.25 * self.dvar2))
        backtrack = ~promote & ((-delta * np.abs(delta) > 4 * sff * dbf)
                                | (-self.davg1 * np.abs(self.davg1) > 4 * self.dvar1)
                                | (-self.davg2 * np.abs(self.davg2) > 4 * self.dvar2))
        if promote.any():
            self.Wf[promote] = self.W[promote]
            # Cross-fade into the promoted filter's output within the block,
            # or the switch clicks.
            e_out = e_fg.copy()
            y_out = y_fg.copy()
            e_out[promote] = self.fade_out * e_fg[promote] + self.fade_in * e_bg[promote]
            y_out[promote] = self.fade_out * y_fg[promote] + self.fade_in * y_bg[promote]
        if backtrack.any():
            self.W[backtrack] = self.Wf[backtrack]
            e_bg = e_bg.copy()
            y_bg = y_bg.copy()
            e_bg[backtrack] = e_fg[backtrack]
            y_bg[backtrack] = y_fg[backtrack]
            see = np.where(backtrack, sff, see)
        reset = promote | backtrack
        for a in (self.davg1, self.davg2, self.dvar1, self.dvar2):
            a[reset] = 0.0

        # Spectra of the background error and echo estimate, zero-padded as
        # overlap-save needs for the gradient.
        self.pad[0, :, n:] = e_bg
        self.pad[1, :, n:] = y_bg
        EY = np.fft.rfft(self.pad, axis=-1)
        E = EY[0]
        rf = E.real ** 2 + E.imag ** 2
        yp = EY[1].real ** 2 + EY[1].imag ** 2

        # Leak: the regression of error power on echo-estimate power across
        # bins, i.e. how much of the echo estimate is still left in the error.
        de = rf - self.Eh
        dy = yp - self.Yh
        pey = np.einsum("mf,mf->m", de, dy)
        pyy = np.sqrt(np.einsum("mf,mf->m", dy, dy))
        pey = pey / (pyy + _TINY)
        self.Eh += self.spec_average * (rf - self.Eh)
        self.Yh += self.spec_average * (yp - self.Yh)
        syy = np.einsum("mn,mn->m", y_bg, y_bg)
        sey = np.einsum("mn,mn->m", e_bg, y_bg)
        alpha = np.minimum(self.beta0 * syy, self.beta_max * see) / (see + _TINY)
        self.Pey = (1 - alpha) * self.Pey + alpha * pey
        self.Pyy = np.maximum((1 - alpha) * self.Pyy + alpha * pyy, _TINY)
        self.Pey = np.clip(self.Pey, self.MIN_LEAK * self.Pyy, self.Pyy)
        self.leak = self.Pey / self.Pyy

        if not self.adapted:
            # Nothing to estimate a residual from yet: a fixed, moderate step
            # until the filter has had its partitions' worth of adaptation.
            mu = np.full((self.m, 1), 0.25)
            self.sum_adapt += 0.25
            if self.sum_adapt > self.p:
                self.adapted = True
        else:
            # Valin's optimal step: residual echo over error, per bin. When
            # someone near the node talks, the error grows and the residual
            # estimate does not, so the step falls without a detector.
            rer = 3.0 * self.leak * syy / (see + _TINY)
            rer = np.clip(np.maximum(rer, sey * sey / (see * syy + _TINY)), 0.0, 0.5)
            e_ = rf + 1.0
            r = np.minimum(self.leak[:, None] * yp, 0.5 * e_)
            mu = (0.7 * r + 0.3 * rer[:, None] * e_) / e_

        xp = X.real ** 2 + X.imag ** 2
        if not self.power.any():
            self.power[:] = xp
        else:
            self.power += self.ss * (xp - self.power)
        step = mu / (self.p * self.power + self.reg)
        self.W += (step * E)[:, None, :] * self.X.conj()[None]
        # The gradient constraint: each partition may only hold `block` taps,
        # or the filter converges to a circular convolution it cannot use.
        w = np.fft.irfft(self.W, n=2 * n, axis=-1)
        w[..., n:] = 0.0
        self.W = np.fft.rfft(w, axis=-1)

        self.sdd += 0.05 * (float(np.einsum("mn,mn->", d, d)) - self.sdd)
        self.see += 0.05 * (float(np.einsum("mn,mn->", e_out, e_out)) - self.see)
        return e_out, y_out


class NoiseTracker:
    """Noise power per bin from the speech presence probability (Gerkmann and
    Hendriks, "Unbiased MMSE-based noise power estimation with low complexity
    and low tracking delay", 2012). `update` takes one frame's power spectrum;
    `noise` is the estimate to use."""

    XI = 10 ** (15 / 10)  # the a priori SNR a bin is assumed to have when speech is present
    FLOOR = 1e-6
    # Fed noise alone, the recursion in `update` settles at 0.76 of the true
    # noise power (simulated: 20000 frames of exponential periodograms), and
    # measured 0.745 on the synthetic scenes. Left uncorrected, the fraction
    # of noise bins that looked like speech averaged 8.5% instead of 2.7% and
    # peaked at 16%, and noise frames were taken for speech. The recursion
    # keeps its own estimate; what leaves the class is corrected.
    SETTLES_AT = 0.76
    # The first frames seed the estimate as their plain average. Seeded from
    # one frame, and the front-end's first frame is half the zeros its window
    # starts from, the estimate began 3 dB low and took over a second to
    # climb: measured, the whole first second of a scene looked like speech.
    WARMUP = 6

    def __init__(self, bins: int):
        self.bins = bins
        self.reset()

    def reset(self) -> None:
        self.var: np.ndarray | None = None
        self.pbar = np.zeros(self.bins)
        self.frames = 0

    @classmethod
    def presence(cls, gamma: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + (1.0 + cls.XI) * np.exp(-gamma * (cls.XI / (1.0 + cls.XI))))

    def update(self, power: np.ndarray) -> None:
        if self.var is None or self.var.max() <= self.FLOOR:
            # Digital silence is not a noise floor: every real sound would
            # be miles above it. Start again from the next frame with energy.
            self.frames = 0
        if self.frames < self.WARMUP:
            self.frames += 1
            if self.frames == 1:
                self.var = np.maximum(power, self.FLOOR)
            else:
                self.var = np.maximum(self.var + (power - self.var) / self.frames, self.FLOOR)
            return
        p = self.presence(power / self.var)
        self.pbar = 0.9 * self.pbar + 0.1 * p
        # Stagnation guard: a bin that has claimed speech for ~0.7 s is let
        # through as noise, slowly, or a step up in noise is never followed.
        p = np.where(self.pbar > 0.99, np.minimum(p, 0.99), p)
        est = (1.0 - p) * power + p * self.var
        self.var = np.maximum(0.8 * self.var + 0.2 * est, self.FLOOR)

    @property
    def noise(self) -> np.ndarray:
        return self.var / self.SETTLES_AT


def mic_positions(spacing_m: float) -> np.ndarray:
    """Three microphones on an equilateral triangle of side `spacing_m`,
    counter-clockwise from +x, centred on the origin. (3, 2) metres."""
    r = spacing_m / math.sqrt(3.0)
    a = np.deg2rad([0.0, 120.0, 240.0])
    return np.stack((r * np.cos(a), r * np.sin(a)), axis=1)


class Beamformer:
    """MVDR over the echo-cancelled microphones, steered by a delay model
    fitted to the generalised eigenvectors of speech against noise.

    `observe` feeds one STFT frame (mics, bins) with the frame's speech/noise
    decision; `refresh` recomputes the steering and weights when new
    statistics have arrived. `w` holds the weights in use, (bins, mics),
    applied as w^H z."""

    ALPHA_NOISE = 0.984   # ~1 s of noise frames
    ALPHA_SPEECH = 0.984  # ~1 s of speech frames
    # Frames of noise before a steering fit is attempted. Without them the
    # generalised eigenvector degenerates to the plain principal one, which
    # finds the loudest directional source: measured, a point noise 2 dB above
    # the talker won the beam for the first four seconds.
    MIN_NOISE_FIT = 15    # ~0.25 s
    MIN_NOISE = 30        # frames (~0.5 s) before MVDR is trusted over delay-and-sum
    MIN_SPEECH = 12       # frames (~0.2 s) of speech before a steering fit
    # Diagonal loading, relative to the mean noise power per bin. Measured on
    # `say` speech against a low-pass point noise at 5 dB SNR, free field:
    # +16.2 dB at 0.002, +14.8 at 0.005, +13.4 at 0.01, +11.9 at 0.02. In
    # simulated reverberation (direct-to-reverberant +6 to -6 dB) the spread
    # over the same range was 1.1 dB at most, and with the gains fitted the
    # talker's level did not depend on it. 0.005 keeps most of the gain and
    # some margin for what a simulation does not model.
    LOADING = 0.005
    # The fit whitens by the noise covariance and wants it as measured: with
    # the true covariances of a scene, loading of 0.02 there moved a fitted
    # delay by 1/16 sample, and 0.001 by nothing. It is there only to keep the
    # Cholesky factorisation defined.
    FIT_LOADING = 0.001
    UPSAMPLE = 16         # delay resolution of the fit: 1/16 sample
    FIT_LO_HZ, FIT_HI_HZ = 150.0, 6000.0

    def __init__(self, rate: int, nfft: int, mics: int, spacing_m: float):
        self.rate, self.nfft, self.m = rate, nfft, mics
        self.bins = nfft // 2 + 1
        freqs = np.arange(self.bins) * rate / nfft
        self.omega = 2 * np.pi * np.arange(self.bins) / nfft  # rad/sample
        self.fit_band = (freqs >= self.FIT_LO_HZ) & (freqs <= min(self.FIT_HI_HZ, rate / 2 * 0.9))
        self.gain_band = (freqs >= 200.0) & (freqs <= min(4000.0, rate / 2 * 0.9))
        # A plane wave cannot arrive with more delay than the spacing allows;
        # 15% of slack for a board that is not exactly to drawing.
        self.tau_max = spacing_m / SPEED_OF_SOUND * rate * 1.15
        u = self.UPSAMPLE
        self.nlag = int(math.ceil(self.tau_max * u))
        size = nfft * u
        self.lag_idx = np.r_[size - self.nlag:size, 0:self.nlag + 1]
        self.lags = np.arange(-self.nlag, self.nlag + 1) / u
        if mics == 3:
            # The third delay is the difference of the other two, and it too
            # is bounded by the spacing.
            li = self.lags[:, None]
            lj = self.lags[None, :]
            self.feasible = np.abs(lj - li) <= self.tau_max
            pos = mic_positions(spacing_m)
            self.baseline = pos[1:] - pos[0]  # (2, 2), metres
        self.eye = np.eye(mics)
        self.reset()

    def reset(self) -> None:
        f, m = self.bins, self.m
        self.Rn = np.zeros((f, m, m), complex)
        self.cn = np.zeros(f)
        self.Ry = np.zeros((f, m, m), complex)
        self.cy = np.zeros(f)
        self.n_noise = 0.0
        self.n_speech = 0.0
        self.new_noise = False
        self.new_speech = False
        self.tau: np.ndarray | None = None  # fitted delays, samples, mic 1 = 0
        self.gains = np.ones(m)             # fitted gains, mic 1 = 1
        self.confidence = 0.0
        self.steer: np.ndarray | None = None
        self.target = np.full((f, m), 1.0 / m, complex)
        self.w = self.target.copy()
        self.mode = "average" if m > 1 else "single"
        self.gain_db: float | None = None

    def observe(self, Z: np.ndarray, speech: bool, noise: bool) -> None:
        if self.m == 1 or not (speech or noise):
            return
        zt = Z.T
        outer = zt[:, :, None] * zt.conj()[:, None, :]
        # Whole frames, not bins weighted by their own speech presence: that
        # weighting correlates with the noise's own magnitude, leaving noise in
        # speech-minus-noise, and it pulled the fitted direction 16 degrees
        # towards a point noise source. Bins between harmonics are dealt with
        # by the fit's weights instead.
        if speech:
            a = 1.0 - self.ALPHA_SPEECH
            self.Ry += a * (outer - self.Ry)
            self.cy += a * (1.0 - self.cy)
            self.n_speech += 1
            self.new_speech = True
        else:
            a = 1.0 - self.ALPHA_NOISE
            self.Rn += a * (outer - self.Rn)
            self.cn += a * (1.0 - self.cn)
            self.n_noise += 1
            self.new_noise = True

    def _noise(self) -> np.ndarray:
        """The noise covariance, debiased for the exponential average's start."""
        return self.Rn / np.maximum(self.cn, _TINY)[:, None, None]

    def _loaded(self, rn: np.ndarray, loading: float) -> np.ndarray:
        tr = np.einsum("fii->f", rn).real / self.m
        return rn + (loading * tr + NoiseTracker.FLOOR)[:, None, None] * self.eye

    def _fit(self, rn: np.ndarray) -> None:
        ry = self.Ry / np.maximum(self.cy, _TINY)[:, None, None]
        L = np.linalg.cholesky(self._loaded(rn, self.FIT_LOADING))
        Li = np.linalg.inv(L)
        A = Li @ ry @ Li.conj().swapaxes(-1, -2)
        lam, U = np.linalg.eigh(A)
        lmax = lam[:, -1]
        h = (L @ U[:, :, -1:])[:, :, 0]  # relative transfer function, up to scale
        d = h[:, 1:] / (h[:, :1] + _TINY)
        # Weight each bin by its share of speech: the generalised eigenvalue is
        # 1 + the bin's SNR at the array, so (l-1)/l is a Wiener-like weight.
        wt = np.where(self.fit_band, np.clip((lmax - 1.0) / np.maximum(lmax, _TINY), 0.0, 1.0), 0.0) ** 2
        total = wt.sum()
        if total < 2.0:
            return  # too few bins hold speech to say anything
        B = wt[:, None] * d / (np.abs(d) + _TINY)
        # Re sum_f B_f exp(j w_f tau) for every tau on a 1/16-sample grid, as
        # one zero-padded inverse FFT per microphone: the weighted GCC.
        S = np.fft.irfft(B.T, n=self.nfft * self.UPSAMPLE, axis=-1)[:, self.lag_idx]
        S *= self.nfft * self.UPSAMPLE / 2.0
        if self.m == 3:
            joint = np.where(self.feasible, S[0][:, None] + S[1][None, :], -np.inf)
            i, j = np.unravel_index(int(np.argmax(joint)), joint.shape)
            best = float(joint[i, j])
            tau = np.array([0.0, self.lags[i], self.lags[j]])
        else:
            k = np.argmax(S, axis=1)
            best = float(S[np.arange(self.m - 1), k].sum())
            tau = np.concatenate(([0.0], self.lags[k]))
        confidence = best / ((self.m - 1) * total)
        if confidence < 0.25:
            return  # the delay model does not explain the eigenvectors
        self.tau = tau
        self.confidence = confidence
        # One broadband gain per microphone as well, from the same weighted
        # bins, averaged in log so that a room mode in one bin cannot drag it.
        # Without it, microphones 2 dB apart in sensitivity cost the talker
        # 2.9 dB of level through the beam (6.4 dB at lighter loading); with
        # it, nothing measurable.
        logmag = (wt[:, None] * np.log(np.abs(d) + _TINY)).sum(axis=0) / total
        self.gains = np.concatenate(([1.0], np.clip(np.exp(logmag), 0.5, 2.0)))
        self.steer = self.gains[None, :] * np.exp(-1j * self.omega[:, None] * tau[None, :])

    def refresh(self) -> None:
        if self.m == 1:
            self.target = np.ones((self.bins, 1), complex)
            self.w = self.target
            return
        if not (self.new_speech or self.new_noise):
            return
        rn = self._noise()
        if self.new_speech and self.n_speech >= self.MIN_SPEECH and self.n_noise >= self.MIN_NOISE_FIT:
            self._fit(rn)
        self.new_speech = self.new_noise = False
        if self.steer is None:
            return
        if self.n_noise >= self.MIN_NOISE:
            x = np.linalg.solve(self._loaded(rn, self.LOADING), self.steer[:, :, None])[:, :, 0]
            den = np.einsum("fm,fm->f", self.steer.conj(), x).real
            self.target = x / np.maximum(den, _TINY)[:, None]
            self.mode = "mvdr"
            b = self.gain_band
            out = np.einsum("fi,fij,fj->f", self.target[b].conj(), rn[b], self.target[b]).real
            ref = np.einsum("fii->f", rn[b]).real / self.m
            self.gain_db = 10 * math.log10((ref.sum() + _TINY) / (out.sum() + _TINY))
        else:
            # Distortionless for the fitted steering: w^H d = 1.
            self.target = self.steer / (np.abs(self.steer) ** 2).sum(axis=1, keepdims=True)
            self.mode = "delay-and-sum"

    def smooth(self) -> None:
        # Glide towards new weights over ~3 blocks rather than jump: a jump in
        # the weights is a jump in the output's colour, audible as a click.
        self.w += 0.35 * (self.target - self.w)

    def direction(self) -> float | None:
        if self.m != 3 or self.tau is None or self.confidence < 0.4:
            return None
        # Relative delays of a plane wave: tau_k = -(p_k - p_0) . s * rate,
        # with s the slowness vector, u * cos(elevation) / c.
        s = np.linalg.solve(self.baseline, -self.tau[1:] / self.rate)
        speed = float(np.hypot(*s)) * SPEED_OF_SOUND
        if not 0.35 <= speed <= 1.3:
            # Near 0 the talker is overhead and has no azimuth; above 1 the
            # delays are not a plane wave across this geometry at all.
            return None
        return float(np.degrees(np.arctan2(s[1], s[0])) % 360.0)


class NoiseSuppressor:
    """Decision-directed Wiener gain over tracked noise plus residual echo."""

    ALPHA_DD = 0.98
    XI_MIN = 10 ** (-25 / 10)

    def __init__(self, bins: int, low_cut: int):
        self.bins = bins
        self.low_cut = low_cut
        self.floor_n = 10 ** (NOISE_FLOOR_DB / 10)
        self.floor_e = 10 ** (ECHO_FLOOR_DB / 10)
        self.tracker = NoiseTracker(bins)
        self.reset()

    def reset(self) -> None:
        self.tracker.reset()
        self.g_prev = np.ones(self.bins)
        self.gamma_prev = np.ones(self.bins)
        self.echo = np.zeros(self.bins)

    def process(self, S: np.ndarray, Se: np.ndarray, leak: float) -> tuple[np.ndarray, np.ndarray]:
        """Returns the gain per bin and the a posteriori SNR per bin, the latter
        against noise AND residual echo, so the echo is not taken for a talker."""
        power = S.real ** 2 + S.imag ** 2
        self.tracker.update(power)
        noise = self.tracker.noise
        # Speex's residual echo model: the leak times the echo estimate, held
        # with a 0.6 decay so the reverberant tail is covered too.
        resid = min(2.0 * leak, 1.0) * (Se.real ** 2 + Se.imag ** 2)
        self.echo = np.maximum(0.6 * self.echo, resid)
        lam = noise + self.echo + _TINY
        gamma = power / lam
        xi = (self.ALPHA_DD * self.g_prev ** 2 * self.gamma_prev
              + (1.0 - self.ALPHA_DD) * np.maximum(gamma - 1.0, 0.0))
        xi = np.maximum(xi, self.XI_MIN)
        g = xi / (1.0 + xi)
        floor = np.sqrt((self.floor_n * noise + self.floor_e * self.echo) / lam)
        g = np.maximum(g, floor)
        self.g_prev = g
        self.gamma_prev = gamma
        g = g.copy()
        g[: self.low_cut] = 0.0
        return g, gamma


class FrontEnd:
    """Echo cancellation, beamforming and noise suppression for one node.

    `process` takes int16 (n, channels) as the node sends it and returns int16
    mono. It buffers internally: each call returns every complete 16 ms block,
    so over time the output length tracks the input length to within one
    block. One instance per node; not thread-safe."""

    TAIL_S = 0.128       # echo tail the canceller models
    # Frame decisions use the band's mean a posteriori SNR: power over tracked
    # noise, averaged over 200-4000 Hz, 1.0 for noise alone. Measured over
    # noise-only blocks of white and low-pass noise scenes: median 1.0, 99th
    # percentile 1.31, highest 1.40. Talker blocks of `say` speech at 5-15 dB
    # SNR were above 2 for 61-84% of blocks; counting bins above a threshold
    # instead caught only 17-61%, because real speech has fewer strong
    # harmonics than a synthetic voice.
    SPEECH_SNR = 2.0
    NOISE_SNR = 1.25
    # The residual-echo model is leak x echo estimate, which cannot describe
    # an echo the filter has not learnt yet. Measured on synthetic and on
    # `say` playback: during the first 2 s of a session's first playback the
    # speech flag fired on 6-12% of blocks, and on none after. So the flag
    # stays down while the node plays and the canceller has heard less than
    # this much playback in total.
    WARMUP_S = 2.0
    HANGOVER_S = 0.128   # speech flag hold-over, and the gap before noise statistics resume
    STALE_S = 3.0        # direction is forgotten after this long without speech

    def __init__(self, rate: int = 16000, ref_channel: int = 0,
                 mic_channels: tuple[int, ...] = (1, 2, 3), spacing_m: float = 0.065):
        if not mic_channels:
            raise ValueError("at least one microphone channel is needed")
        self.rate = rate
        self.ref_channel = ref_channel
        self.mic_channels = tuple(mic_channels)
        self.spacing_m = spacing_m
        self.channels = max(ref_channel, *self.mic_channels) + 1
        # 16 ms blocks at any rate, as a power of two for the FFT.
        self.hop = 1 << max(4, round(math.log2(rate * 0.016)))
        self.nfft = 2 * self.hop
        self.latency_s = (2 * self.hop - 1) / rate
        m = len(self.mic_channels)
        bins = self.hop + 1
        k = np.arange(self.nfft)
        self.window = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * k / self.nfft))
        freqs = np.arange(bins) * rate / self.nfft
        self.vad_band = (freqs >= 200.0) & (freqs <= min(4000.0, rate / 2 * 0.9))
        low_cut = int(np.searchsorted(freqs, LOW_CUT_HZ))
        partitions = max(1, math.ceil(self.TAIL_S * rate / self.hop))
        self.aec = EchoCanceller(self.hop, partitions, m, rate)
        self.bf = Beamformer(rate, self.nfft, m, spacing_m)
        self.ns = NoiseSuppressor(bins, low_cut)
        self.pre = NoiseTracker(bins)  # on the microphones' mean, for the beam's statistics
        self.far_floor = self.hop * _dbfs_power(FAR_END_DBFS)
        # A frame's mean power per bin, in the units of a sqrt-Hann STFT, for
        # a signal at SILENCE_DBFS: sum(window**2) = hop.
        self.silence = self.hop * _dbfs_power(SILENCE_DBFS)
        self.tail_blocks = partitions
        self.hang_blocks = max(1, round(self.HANGOVER_S * rate / self.hop))
        self.stale_blocks = round(self.STALE_S * rate / self.hop)
        self.warm_blocks = round(self.WARMUP_S * rate / self.hop)
        self._trace: list | None = None  # tests set a list to record (weights, gain) per block
        self.reset()

    def reset(self) -> None:
        m = len(self.mic_channels)
        self.aec.reset()
        self.bf.reset()
        self.ns.reset()
        self.pre.reset()
        self._pending = np.zeros((0, self.channels))
        self._prev = np.zeros((2, m, self.hop))  # last block of error and echo estimate
        self._frame = np.zeros((2, m, self.nfft))
        self._ola = np.zeros(self.hop)
        self._blocks = 0
        self._far_hang = 0
        self._pre_hang = 0
        self._speech_hang = 0
        self._last_speech = -(10 ** 9)
        self._cpu = 0.0

    # ---- public surface -----------------------------------------------------

    @property
    def direction(self) -> float | None:
        """Azimuth of the talker in degrees, relative to the board (see the
        module's notes), or None: before a talker has been heard, when the
        delays do not fit a plane wave well, or 3 s after speech last ended."""
        if self._blocks - self._last_speech > self.stale_blocks:
            return None
        return self.bf.direction()

    @property
    def speech(self) -> bool:
        """Speech on the cleaned output in the last block, held for 128 ms. It
        excludes residual echo, and stays down during the first 2 s of
        playback a front-end hears, while the canceller is learning the room."""
        return self._speech_hang > 0

    @property
    def stats(self) -> dict:
        """For logs and dashboards:

        seconds          audio processed since the start or the last reset
        rtf              CPU seconds per second of audio, this thread
        latency_s        worst-case algorithmic latency
        erle_db          echo return loss enhancement over recent playback; it
                         reads low while someone talks over the playback
        echo_leak_db     the canceller's own estimate of the echo left in
                         its output, relative to the echo it removed
        far_end          the node's speaker is playing (or its tail is)
        beamformer       "average", "delay-and-sum", "mvdr", or "single"
        beam_gain_db     noise reduction of the beam against one microphone,
                         predicted from the tracked noise covariance
        delays_samples   fitted delay of each microphone against the first
        fit_confidence   how well those delays explain the eigenvectors, 0-1
        noise_frames, speech_frames   statistics gathered so far
        """
        seconds = self._blocks * self.hop / self.rate
        return {
            "seconds": seconds,
            "rtf": self._cpu / seconds if seconds else None,
            "latency_s": self.latency_s,
            "erle_db": self.aec.erle_db,
            "echo_leak_db": (10 * math.log10(float(self.aec.leak.mean()))
                             if self.aec.active_blocks else None),
            "far_end": self._far_hang > 0,
            "beamformer": self.bf.mode,
            "beam_gain_db": self.bf.gain_db,
            "delays_samples": None if self.bf.tau is None else [float(t) for t in self.bf.tau],
            "fit_confidence": float(self.bf.confidence),
            "noise_frames": int(self.bf.n_noise),
            "speech_frames": int(self.bf.n_speech),
        }

    def process(self, frames: np.ndarray) -> np.ndarray:
        frames = np.asarray(frames)
        if frames.ndim != 2 or frames.shape[1] < self.channels:
            raise ValueError(f"expected (n, >= {self.channels}) interleaved samples, got {frames.shape}")
        buf = np.concatenate((self._pending, frames[:, : self.channels].astype(np.float64)))
        blocks = len(buf) // self.hop
        out = np.empty(blocks * self.hop)
        start = time.thread_time()
        for b in range(blocks):
            blk = buf[b * self.hop:(b + 1) * self.hop]
            out[b * self.hop:(b + 1) * self.hop] = self._block(blk)
        self._cpu += time.thread_time() - start
        self._pending = buf[blocks * self.hop:]
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16)

    # ---- one block ----------------------------------------------------------

    def _block(self, blk: np.ndarray) -> np.ndarray:
        n = self.hop
        x = np.ascontiguousarray(blk[:, self.ref_channel])
        d = np.ascontiguousarray(blk[:, self.mic_channels].T)
        active = float(x @ x) > self.far_floor
        self._far_hang = self.tail_blocks if active else max(0, self._far_hang - 1)
        e, y = self.aec.process(d, x, active)

        # One STFT frame of the error and of the echo estimate: the previous
        # block and this one, under the analysis window.
        self._frame[0, :, :n] = self._prev[0]
        self._frame[1, :, :n] = self._prev[1]
        self._frame[0, :, n:] = e
        self._frame[1, :, n:] = y
        self._prev[0] = e
        self._prev[1] = y
        spec = np.fft.rfft(self._frame * self.window, axis=-1)
        Z, Ye = spec[0], spec[1]

        # The beam's own speech/noise decision, on the microphones' mean power
        # so that it cannot chase its own weights.
        pavg = (Z.real ** 2 + Z.imag ** 2).mean(axis=0)
        self.pre.update(pavg)
        band = self.vad_band
        loud = pavg[band].mean() > self.silence
        snr = float((pavg[band] / self.pre.noise[band]).mean())
        is_speech = loud and snr > self.SPEECH_SNR
        self._pre_hang = self.hang_blocks if is_speech else max(0, self._pre_hang - 1)
        # Stricter than "not speech": a quiet syllable that lands in the noise
        # covariance is a direction the beam will then null.
        is_noise = not is_speech and self._pre_hang == 0 and snr < self.NOISE_SNR
        if self._far_hang == 0:
            self.bf.observe(Z, is_speech, is_noise)
        if self._blocks % 4 == 0:
            self.bf.refresh()
        self.bf.smooth()
        w = self.bf.w

        S = np.einsum("fm,mf->f", w.conj(), Z)
        Se = np.einsum("fm,mf->f", w.conj(), Ye)
        g, gamma = self.ns.process(S, Se, float(self.aec.leak.mean()))
        if self._trace is not None:
            self._trace.append((w.copy(), g.copy()))

        power = S.real ** 2 + S.imag ** 2
        learning = self._far_hang > 0 and self.aec.active_blocks < self.warm_blocks
        voiced = (not learning and power[band].mean() > self.silence
                  and float(gamma[band].mean()) > self.SPEECH_SNR)
        self._speech_hang = self.hang_blocks if voiced else max(0, self._speech_hang - 1)
        if is_speech and self._far_hang == 0:
            self._last_speech = self._blocks
        self._blocks += 1

        synth = np.fft.irfft(g * S, n=self.nfft) * self.window
        out = self._ola + synth[:n]
        self._ola = synth[n:].copy()
        if not np.isfinite(out).all():
            # A live stream must never go permanently silent on one bad
            # block: start the whole pipeline again.
            self.reset()
            return np.zeros(n)
        return out

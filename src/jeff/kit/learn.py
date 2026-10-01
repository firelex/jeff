"""Two small linear models in plain numpy for the shortcut report: a softmax classifier (which label, from a few numbers
per row) and an option picker (which option, from a few numbers per option, compared within each row)."""

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]
type IntArray = NDArray[np.int64]

STEPS = 400
RATE = 0.5
L2 = 1e-3


class Scaler:
    """Centre and scale each feature by the training rows' mean and spread (constant features are left at zero)."""

    def __init__(self, features: FloatArray) -> None:
        self.mean = features.mean(axis=0)
        spread = features.std(axis=0)
        self.spread = np.where(spread > 0, spread, 1.0)

    def __call__(self, features: FloatArray) -> FloatArray:
        return np.asarray((features - self.mean) / self.spread, dtype=np.float64)


def softmax(scores: FloatArray) -> FloatArray:
    shifted = np.exp(scores - scores.max(axis=1, keepdims=True))
    return np.asarray(shifted / shifted.sum(axis=1, keepdims=True), dtype=np.float64)


class Classifier:
    """Multinomial logistic regression with balanced class weights, fitted by full-batch gradient descent."""

    def __init__(self, features: FloatArray, labels: IntArray, classes: int) -> None:
        self.scale = Scaler(features)
        x = self.scale(features)
        counts = np.bincount(labels, minlength=classes).astype(np.float64)
        weights = (len(labels) / (classes * np.maximum(counts, 1)))[labels]
        targets = np.eye(classes)[labels]
        self.weights = np.zeros((x.shape[1], classes))
        self.bias = np.zeros(classes)
        for _ in range(STEPS):
            error = (softmax(x @ self.weights + self.bias) - targets) * weights[:, None] / weights.sum()
            self.weights -= RATE * (x.T @ error + L2 * self.weights)
            self.bias -= RATE * error.sum(axis=0)

    def predict(self, features: FloatArray) -> IntArray:
        return np.asarray(np.argmax(self.scale(features) @ self.weights + self.bias, axis=1), dtype=np.int64)


def balanced_accuracy(predicted: IntArray, actual: IntArray) -> float:
    """The mean, over the classes present, of the share of each class's rows predicted correctly."""
    recalls = [float((predicted[actual == value] == value).mean()) for value in np.unique(actual)]
    return float(np.mean(recalls))


class Picker:
    """Conditional logit: each option gets a score from its features, and the options of one row compete through a
    softmax. Rows are given as one feature matrix of all their options plus each row's first option index."""

    def __init__(self, features: FloatArray, starts: IntArray, correct: IntArray) -> None:
        self.scale = Scaler(features)
        x = self.scale(features)
        row_of = np.repeat(np.arange(len(starts)), np.diff(np.append(starts, len(x))))
        self.weights = np.zeros(x.shape[1])
        for _ in range(STEPS):
            probabilities = segment_softmax(x @ self.weights, starts, row_of)
            gradient = (x.T @ probabilities - x[correct].sum(axis=0)) / len(starts)
            self.weights -= RATE * (gradient + L2 * self.weights)

    def pick(self, features: FloatArray, starts: IntArray) -> IntArray:
        """Each row's highest-scoring option, as an index into that row's options."""
        scores = self.scale(features) @ self.weights
        ends = np.append(starts, len(scores))
        return np.asarray([int(np.argmax(scores[start:end])) for start, end in zip(ends[:-1], ends[1:])], dtype=np.int64)


def segment_softmax(scores: FloatArray, starts: IntArray, row_of: IntArray) -> FloatArray:
    highest = np.maximum.reduceat(scores, starts)
    values = np.exp(scores - highest[row_of])
    return np.asarray(values / np.add.reduceat(values, starts)[row_of], dtype=np.float64)

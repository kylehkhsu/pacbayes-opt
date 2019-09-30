import torch
import torch.nn as nn
import torch.nn.functional as F
import math

from .bayesian_layers import BayesianLinear
from .classifier import Classifier
from functools import partial
import numpy as np
import ipdb

neg_half_log_2_pi = -0.5 * math.log(2.0 * math.pi)
softplus = lambda x: math.log(1 + math.exp(x))

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


class BayesianClassifier(nn.Module):
    def __init__(self, bayesian_layers, prob_threshold, normalize_surrogate_by_log_classes, oracle_prior_variance):
        super(BayesianClassifier, self).__init__()
        for i_layer, layer in enumerate(bayesian_layers):
            self.add_module(f'bayesian_layer{i_layer}', layer)

        # TODO: test if 'layer' in name condition yields expected behavior
        self.bayesian_layers = [module for name, module in self.named_modules() if 'bayesian_layer' in name]
        self.prob_threshold = prob_threshold
        self.normalize_surrogate_by_log_classes = normalize_surrogate_by_log_classes
        self.oracle_prior_variance = oracle_prior_variance

    def forward(self, x, mode):
        for layer in self.bayesian_layers:
            x = layer(x, mode)
        return x

    def forward_train(self, x, y, n_samples=1):
        running_kl = 0.0
        running_surrogate = 0.0
        for i in range(n_samples):
            probs = self(x, 'MC')
            kl = self.kl()
            surrogate = BayesianClassifier.surrogate(
                probs=probs,
                y=y,
                prob_threshold=self.prob_threshold,
                normalize_surrogate_by_log_classes=self.normalize_surrogate_by_log_classes
            ).mean()

            running_kl += kl
            running_surrogate += surrogate

        return running_kl / n_samples, running_surrogate / n_samples

    def kl(self):
        net_kl = 0.0
        for layer in self.bayesian_layers:
            if self.oracle_prior_variance:
                net_kl += layer.kl_oracle_prior_variance()
            else:
                net_kl += layer.kl()
        return net_kl

    @staticmethod
    def surrogate(probs, y, prob_threshold, normalize_surrogate_by_log_classes):
        y = y.view([y.shape[0], -1])
        log_likelihood = probs.gather(1, y).clamp(min=prob_threshold, max=1).log()
        surrogate = -log_likelihood
        if normalize_surrogate_by_log_classes:
            n_classes = probs.shape[-1]
            surrogate = surrogate.div(math.log(n_classes))
        return surrogate

    @staticmethod
    def quad_bound(risk, kl, dataset_size, delta):
        log_2_sqrt_n_over_delta = math.log(2 * math.sqrt(dataset_size) / delta)
        fraction = (kl + log_2_sqrt_n_over_delta).div(2 * dataset_size)
        sqrt1 = (risk + fraction).sqrt()
        sqrt2 = fraction.sqrt()
        return (sqrt1 + sqrt2).pow(2)

    # @staticmethod
    # def lambda_bound(risk, kl, dataset_size, delta, lam):
    #     log_2_sqrt_n_over_delta = math.log(2 * math.sqrt(dataset_size) / delta)
    #     term1 = risk.div(1 - lam / 2)
    #     term2 = (kl + log_2_sqrt_n_over_delta).div(dataset_size * lam * (1 - lam / 2))
    #     return term1 + term2

    @staticmethod
    def pinsker_bound(risk, kl, dataset_size, delta):
        B = (kl + math.log(2 * math.sqrt(dataset_size) / delta)).div(dataset_size)
        return risk + B.div(2).sqrt()

    @staticmethod
    def inverted_kl_bound(risk, kl, dataset_size, delta):
        return torch.min(
            BayesianClassifier.quad_bound(risk, kl, dataset_size, delta),
            BayesianClassifier.pinsker_bound(risk, kl, dataset_size, delta)
        )

    def evaluate_on_loader(self, loader):
        training = self.training
        self.eval()

        corrects, totals, surrogates = 0.0, 0.0, 0.0
        with torch.no_grad():
            for x, y in loader:
                x, y = x.to(device), y.to(device)
                probs = self(x, 'MC')
                correct, total = Classifier.evaluate(probs, y)
                surrogate_batch = BayesianClassifier.surrogate(
                    probs=probs,
                    y=y,
                    prob_threshold=self.prob_threshold,
                    normalize_surrogate_by_log_classes=self.normalize_surrogate_by_log_classes
                )
                corrects += correct.item()
                totals += total.item()
                surrogates += surrogate_batch.sum().item()
        error = 1 - corrects / totals
        surrogate = surrogates / totals

        self.train(mode=training)
        return error, surrogate

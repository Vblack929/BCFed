import torch
from math import log
import numpy as np
from fl.models import NumpyModel
from fl.data_utils import sum_model_L2_distance

class ServerAgg():
    def apply_gradients(self, grads):
        raise NotImplementedError()

class FedAvg(ServerAgg):
    def __init__(self, global_model, beta, slr):
        """
        Average the clients' grads
        Args:
        - global_model:              {list of np.ndarray} the global model parameters of last round
        - beta:                      {float} the hyperparameter when the compress mode is on
        - slr:                       {float} the server side learning rate
        """
        self.global_model = NumpyModel(global_model)
        self.beta = beta
        self.lr = slr
        self.m = self.global_model.zeros_like()

    def apply_gradients(self, clients_grads):
        """
        Args:
        - client_grads：             if compress == False:
                                        {list of the list of np.ndarray} the grads of model parameters of a NumpyModel, each contains a client's model updates
                                    if compress == True:
                                        {list of list of np.ndarrays} the compressed grads of each client
        - global_model:              {list of np.ndarray} the global model parameters of last round

        Returns:
            -round_agg      {NumpyModel} the aggregated global model for next round training
        """
        self.clients_grads = clients_grads

        round_agg = self.global_model.zeros_like()
        for client_grads in self.clients_grads:
            round_agg += client_grads
        N = len(self.clients_grads)
        round_agg = round_agg / N

        self.global_model += - self.lr * round_agg
        return self.global_model

class MI(ServerAgg):
    def __init__(self, global_model, unlabeled_data, model, beta, slr, m_prev):
        """
        Args:
        - client_grads：             if compress == False:
                                        {list of the list of np.ndarray} the grads of model parameters of a NumpyModel, each contains a client's model updates
                                    if compress == True:
                                        {list of list of np.ndarrays} the compressed grads of each client
        - global_model:             {list of np.ndarray} the global model parameters of last round
        - unlabeled_data:           {list of tensors} the unlabeled dataset, only contains x, no y included
        - model:                    {FLModel} the current task model
        - beta:                     {float} the hyperparameter when the compress mode is on
        - slr:                      {float} the server side learning rate
        - m_prev:                   {list of np.ndarray} the momentum value of last round
        """

        self.global_model = NumpyModel(global_model)
        self.unlabel = unlabeled_data
        self.model = model
        self.m = NumpyModel(m_prev)
        self.beta = beta
        self.lr = slr

    def apply_gradients(self, clients_grads):
        """
        Filter out potential malicious client's model update and average the rest model updates
        Args:
        - client_grads：    {list of NumpyModel} the grads of model parameters of a NumpyModel, each contains a client's model updates
        Returns:
            -round_agg      {NumpyModel} the aggregated global model for next round training
            -self.m         {list of ndarrays} the momentum value in this round of aggregation
        """
        # server decompress gradients
        self.clients_grads = clients_grads

        self.clients_model = []
        for client_grads in self.clients_grads:
            self.clients_model.append(self.global_model - client_grads) # model parameters of clients models

        # Median based method
        mutual_mis = self.get_mutual_mi()
        MI = mutual_mis

        # select client models, according to the "two-sigma edit" rule
        MAD = np.median(abs(MI - np.median(MI)))  # get the median absolute deviation from median of MI values
        MADN = MAD / 0.6745  # get the normalized MAD values, note 0.6745 is the MAD of a standard normal distribution

        ts = (MI - np.median(MI)) / MADN
        select = []
        for i in range(len(ts)):
            if abs(ts[i]) < 2:
                select.append(self.clients_grads[i])


        round_agg = self.global_model.zeros_like()
        for client_grads in select:
            round_agg += client_grads
        N = len(select)
        round_agg = round_agg / N

        # Momentum update, Nesterov Momentum
        m_prev = self.m
        self.m = self.beta * m_prev - self.lr * round_agg
        self.global_model += - self.beta * m_prev + (1 + self.beta) * self.m

        return self.global_model, self.m

    def get_mutual_mi(self):
        """
        Get the mutual information value between client model and global model
        Returns:
            - MI:                       (np.ndarray} the corresponding MI value between client model and golobal model
        """
        mutual_mi = np.zeros(shape=(len(self.clients_model), len(self.clients_model)))
        for i in range(len(self.clients_model)):
            self.model.set_params(self.clients_model[i])
            client_out_i = self.model.forward(self.unlabel)
            client_exp_i = client_out_i.mean(dim=1, keepdim=True) # the expectation of model output
            for j in range(i+1, len(self.clients_model)):
                self.model.set_params(self.clients_model[j])
                client_out_j = self.model.forward(self.unlabel)
                client_exp_j = client_out_j.mean(dim=1, keepdim=True)  # the expectation of model output

                rho = torch.sum((client_out_i - client_exp_i) * (client_out_j - client_exp_j), dim=1) / \
                      torch.sqrt(torch.sum(torch.square((client_out_i - client_exp_i)), dim=1) * \
                                 torch.sum(torch.square(client_out_j - client_exp_j), dim=1))

                intermediate = 1 - pow(rho.mean().item(), 2)
                # prevent math domain error
                if intermediate < 1e-300:
                    intermediate += 1e-300
                try:
                    log(intermediate)
                except ValueError:
                    print("the intermediate value is ",intermediate)
                mi = - log(intermediate) / 2

                mutual_mi[i, j] = mi
                mutual_mi[j, i] = mi
        avg_mi = np.mean(mutual_mi, axis=1)
        return avg_mi

class Bicotti(ServerAgg):
    def __init__(self, global_model, beta, slr):
        """
        Average the clients' grads
        Args:
            - global_model:              {list of np.ndarray} the global model parameters of last round
            - beta:                      {float} the hyperparameter when the compress mode is on
            - slr:                       {float} the server side learning rate
        """
        self.global_model = NumpyModel(global_model)
        self.beta = beta
        self.lr = slr
        self.m = self.global_model.zeros_like()

    def apply_gradients(self, clients_grads):
        """
        Filter out potential malicious client's model update and average the rest model updates
        Args:
            - client_grads：             if compress == False:
                                        {list of the list of np.ndarray} the grads of model parameters of a NumpyModel, each contains a client's model updates
                                    if compress == True:
                                        {list of list of np.ndarrays} the compressed grads of each client
            - global_model:              {list of np.ndarray} the global model parameters of last round

        Returns:
            -round_agg      {NumpyModel} the aggregated global model for next round training
        """

        self.clients_grads = clients_grads

        # Euclidean distance calculation
        dists = np.zeros(shape=(len(self.clients_grads), len(self.clients_grads)))
        for i in range(len(self.clients_grads)):
            for j in range(i+1, len(self.clients_grads)):
                dist = sum_model_L2_distance(self.clients_grads[i], self.clients_grads[j])
                dists[i, j] = dist
                dists[j, i] = dist

        dists = np.sort(dists)
        f = int(len(self.clients_grads) / 3)    # the theoretic f value
        R = len(self.clients_grads) # the total number of updates
        select = R - f - 2
        scores = np.sum(dists[:, :select+1], axis=1) # sum value of Euclidean distance of closet R-f-2 updates
        scores_sort_idx = np.argsort(scores)

        round_agg = self.global_model.zeros_like()
        # Select the R-f clients with the lowest scores
        for i in range(R - f):
            round_agg += self.clients_grads[scores_sort_idx[i]]
        N = R - f
        round_agg = round_agg / N

        self.global_model += - self.lr * round_agg
        return self.global_model
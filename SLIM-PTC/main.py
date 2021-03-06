import sys
import os
import torch
import random
import numpy as np
from tqdm import tqdm
from torch.autograd import Variable
from torch.nn.parameter import Parameter
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import math
from Clustering import Clustering
from graphVec import graphVec
import pdb
from predict import MLPClassifier, MLPRegression
from sklearn import metrics

from util import cmd_args, load_data
from pytorch_util import weights_init, gnn_spmm
from sklearn.cluster import KMeans
import matplotlib
matplotlib.use("agg")
import matplotlib.pyplot as plt

class SLIM(nn.Module):
    def __init__(self, output_dim, num_node_feats, num_edge_feats, latent_dim=[32, 32, 32, 1], k=30, conv1d_channels=[16, 32], conv1d_kws=[0, 5], conv1d_activation='ReLU'):

        super(SLIM, self).__init__()
        self.latent_dim = latent_dim
        self.output_dim = output_dim
        self.num_node_feats = num_node_feats
        self.num_edge_feats = num_edge_feats
        self.k = k
        self.total_latent_dim = sum(latent_dim)
        conv1d_kws[0] = self.total_latent_dim

        self.conv_params = nn.ModuleList()
        self.conv_params.append(nn.Linear(num_node_feats + num_edge_feats, latent_dim[0]))
        for i in range(1, len(latent_dim)):
            self.conv_params.append(nn.Linear(latent_dim[i-1], latent_dim[i]))

        self.conv1d_params1 = nn.Conv1d(1, conv1d_channels[0], conv1d_kws[0], conv1d_kws[0])

        self.conv1d_params1qkq=nn.Conv1d(7, conv1d_channels[0], 5, 5)
        self.conv1d_params1PU = nn.Conv1d(7, conv1d_channels[0], 1, 1)
        self.maxpool1d = nn.MaxPool1d(2, 2)
        self.maxpool1dqkq = nn.MaxPool1d(2, 2)
        self.maxpool1dPU = nn.MaxPool1d(2, 2)
        self.conv1d_params2 = nn.Conv1d(conv1d_channels[0], conv1d_channels[1], conv1d_kws[1], 1)
        self.conv1d_params2qkq = nn.Conv1d(conv1d_channels[0], conv1d_channels[1], conv1d_kws[1], 1)
        self.conv1d_params2PU = nn.Conv1d(conv1d_channels[0], conv1d_channels[1], conv1d_kws[1], 1)
        dense_dim = int((k - 2) / 2 + 1)
        self.dense_dim = (dense_dim - conv1d_kws[1] + 1) * conv1d_channels[1]
        if output_dim > 0:
            self.out_params = nn.Linear(self.dense_dim, output_dim)

        self.conv1d_activation = eval('nn.{}()'.format(conv1d_activation))
        self.conv1d_activationqkq = eval('nn.{}()'.format(conv1d_activation))
        weights_init(self)


class Classifier(nn.Module):
    def __init__(self, regression=False):
        super(Classifier, self).__init__()
        self.regression = regression
        if cmd_args.gm == 'SLIM':
            model = SLIM
        else:
            print('unknown gm %s' % cmd_args.gm)
            sys.exit()

        if cmd_args.gm == 'SLIM':
            self.gnn = model(latent_dim=cmd_args.latent_dim,
                             output_dim=cmd_args.out_dim,
                             num_node_feats=cmd_args.feat_dim + cmd_args.attr_dim,
                             num_edge_feats=cmd_args.edge_feat_dim,
                             k=cmd_args.sortpooling_k,
                             conv1d_activation=cmd_args.conv1d_activation)
        out_dim = cmd_args.out_dim
        if out_dim == 0:
            if cmd_args.gm == 'SLIM':
                out_dim = self.gnn.dense_dim
            else:
                out_dim = cmd_args.latent_dim
        self.mlp = MLPClassifier(input_size=out_dim, hidden_size=cmd_args.hidden, num_class=cmd_args.num_class,
                                 with_dropout=cmd_args.dropout)
        if regression:
            self.mlp = MLPRegression(input_size=out_dim, hidden_size=cmd_args.hidden, with_dropout=cmd_args.dropout)

    def PrepareFeatureLabel(self, batch_graph):
        if self.regression:
            labels = torch.FloatTensor(len(batch_graph))
        else:
            labels = torch.LongTensor(len(batch_graph))
        n_nodes = 0

        if batch_graph[0].node_tags is not None:
            node_tag_flag = True
            concat_tag = []
        else:
            node_tag_flag = False

        if batch_graph[0].node_features is not None:
            node_feat_flag = True
            concat_feat = []
        else:
            node_feat_flag = False

        if cmd_args.edge_feat_dim > 0:
            edge_feat_flag = True
            concat_edge_feat = []
        else:
            edge_feat_flag = False

        for i in range(len(batch_graph)):
            labels[i] = batch_graph[i].label
            n_nodes += batch_graph[i].num_nodes
            if node_tag_flag == True:
                concat_tag += batch_graph[i].node_tags
            if node_feat_flag == True:
                tmp = torch.from_numpy(batch_graph[i].node_features).type('torch.FloatTensor')
                concat_feat.append(tmp)
            if edge_feat_flag == True:
                if batch_graph[i].edge_features is not None:  # in case no edge in graph[i]
                    tmp = torch.from_numpy(batch_graph[i].edge_features).type('torch.FloatTensor')
                    concat_edge_feat.append(tmp)

        if node_tag_flag == True:
            concat_tag = torch.LongTensor(concat_tag).view(-1, 1)
            node_tag = torch.zeros(n_nodes, cmd_args.feat_dim)
            node_tag.scatter_(1, concat_tag, 1)

        if node_feat_flag == True:
            node_feat = torch.cat(concat_feat, 0)

        if node_feat_flag and node_tag_flag:
            # concatenate one-hot embedding of node tags (node labels) with continuous node features
            node_feat = torch.cat([node_tag.type_as(node_feat), node_feat], 1)
        elif node_feat_flag == False and node_tag_flag == True:
            node_feat = node_tag
        elif node_feat_flag == True and node_tag_flag == False:
            pass
        else:
            node_feat = torch.ones(n_nodes, 1)  # use all-one vector as node features

        if edge_feat_flag == True:
            edge_feat = torch.cat(concat_edge_feat, 0)

        if cmd_args.mode == 'gpu':
            node_feat = node_feat.cuda()
            labels = labels.cuda()
            if edge_feat_flag == True:
                edge_feat = edge_feat.cuda()

        if edge_feat_flag == True:
            return node_feat, edge_feat, labels
        return node_feat, labels


    def forward(self,selected_idx, batch_graph,batch_graph_sub,adj_one,node_feat_all,q,u,bin1,g_list_test,pos,codestrain):

        if len(batch_graph)==310:

           if pos <310:
               batch_graph = batch_graph
           else:
               batch_graph = g_list_test
        else:

            batch_graph = batch_graph


        feature_label = self.PrepareFeatureLabel(batch_graph)
        feature_label_sub = self.PrepareFeatureLabel(batch_graph_sub)
        labels_sub=0
        if len(feature_label) == 2:
            node_feat, labels = feature_label
            edge_feat = None
        elif len(feature_label) == 3:
            node_feat, edge_feat, labels = feature_label
        if len(feature_label_sub) == 2:
                node_feat, labels1 = feature_label_sub
                edge_feat = None
        elif len(feature_label_sub) == 3:
                node_feat, edge_feat, labels1 = feature_label_sub

        count = 0
        graph_sizesall = [batch_graph[i].num_nodes for i in range(len(batch_graph))]
        for i in range(int(selected_idx[0])):
            count = graph_sizesall[i] + count

        count1=graph_sizesall[int(selected_idx[0])] + count

        deltacount=count1-count
        adjsub = np.zeros([deltacount, deltacount])

        for ii in range(deltacount):
            for jj in range(len(adj_one[int(selected_idx[0])][ii])):
                adjsub[ii][adj_one[int(selected_idx[0])][ii][jj]] = 1


        q_sub=q[count:count1,:]
        q_sub_bin=torch.sum(q_sub, 0)

        bin=np.zeros((graph_sizesall[int(selected_idx[0])],num_centers))
        bin11=q_sub_bin

        norm_adjsub = np.linalg.norm(adjsub, axis=1)
        adjsub = adjsub / np.sqrt(norm_adjsub+0.001)
        adjsub1 = np.sum(adjsub, axis=1)
        d_adjsub = np.diag(adjsub1)
        from numpy import linalg as la
        v, Q = la.eig(d_adjsub)
        V = np.diag(v ** (-0.5))
        T = Q * V * la.inv(Q)
        T_adjsub = np.dot(T, adjsub)
        T_adjsub_T = np.dot(T_adjsub, T)
        kz = T_adjsub_T

        kz= torch.from_numpy(kz).type(torch.FloatTensor).cuda()

        kzsquare2=torch.mm(kz, kz)
        kzsquare3=torch.mm(kz,kzsquare2)

        q_sub=q_sub.cuda()
        qkzsquare2 = torch.mm(q_sub.t(), kzsquare2)
        qkzsquare3 = torch.mm(q_sub.t(), kzsquare3)

        qkq_square2 = torch.mm(qkzsquare2, q_sub)
        qkq_square3 = torch.mm(qkzsquare3, q_sub)
        qk = torch.mm(q_sub.t(), kz)
        qkq = torch.mm(qk, q_sub)
        labels=labels[selected_idx]
        bin = torch.from_numpy(bin).type(torch.FloatTensor)
        bin= bin.cuda()
        qkq2=qkq_square2
        qkq3 =qkq_square3
        graph_sizesall = [batch_graph[i].num_nodes for i in range(len(batch_graph))]
        count = 0
        for i in range(int(selected_idx[0])):
            count = graph_sizesall[i] + count
        node_feat_all = node_feat_all[count:graph_sizesall[int(selected_idx[0])] + count, :]
        node_feat_all = torch.from_numpy(node_feat_all).type(torch.FloatTensor)
        return self.mlp( node_feat_all, bin, qkq, qkq2, qkq3, q_sub, bin11, labels)

    def output_features(self, batch_graph):
        feature_label = self.PrepareFeatureLabel(batch_graph)
        if len(feature_label) == 2:
            node_feat, labels = feature_label
            edge_feat = None
        elif len(feature_label) == 3:
            node_feat, edge_feat, labels = feature_label
        embed = self.gnn(batch_graph, node_feat, edge_feat)
        return embed, labels

    def embed_features(self, batch_graph):
        feature_label = self.PrepareFeatureLabel(batch_graph)
        if len(feature_label) == 2:
            node_feat, labels = feature_label
            edge_feat = None
        elif len(feature_label) == 3:
            node_feat, edge_feat, labels = feature_label
        embed = self.gnn(batch_graph, node_feat, edge_feat)
        embed=embed.cpu().detach().numpy()
        labels=labels.cpu().detach().numpy()
        return embed, labels





def trainloop_dataset(node_feat_new6,Dict,epoch,W,Uw,adj_one,g_list,adj_one_test,g_list_test, classifier, sample_idxes, sample_test_idxes, optimizer=None, bsize=cmd_args.batch_size):
    total_loss = []
    total_iters = (len(sample_idxes) + (bsize - 1) * (optimizer is None)) // bsize
    pbar = tqdm(range(total_iters), unit='batch')
    all_targets = []
    all_scores = []

    n_samples = 0

    node_feat = node_feat_new6
    node_feat = torch.mm(node_feat, Dict)
    node_feat = torch.relu(node_feat)
    node_feat = node_feat.detach().numpy()
    z = node_feat[0:8792]

    if epoch==0:
       print("Clustering..")
       kmeans = KMeans(n_clusters=num_centers, n_init=100,max_iter=100)
       kmeans.fit_predict(z)
       Uw = torch.from_numpy(kmeans.cluster_centers_).type(torch.FloatTensor)
    kl_loss, q = Clustering(z, Uw, Dict)
    loss1 =  kl_loss



    codestrain = []
    for pos in pbar:


        selected_idx = sample_idxes[pos * bsize: (pos + 1) * bsize]
        batch_graph = [g_list[idx] for idx in selected_idx]
        targets = [g_list[idx].label for idx in selected_idx]

        all_targets += targets
        if classifier.regression:
            pred, mae, loss = classifier(g_list)
            all_scores.append(pred.cpu().detach())  # for binary classification
        else:

            logits, loss, acc = classifier(selected_idx,g_list,batch_graph,adj_one,node_feat1,q,Uw,bin,g_list_test,pos,codestrain)
            all_scores.append(logits[:, 1].cpu().detach())  # for binary classification

        loss1 = loss1.cuda()

        ##klloss and classification loss
        loss = 0.1*loss1 + loss

        if optimizer is not None:
            optimizer.zero_grad()
            loss.backward(retain_graph=True)
            optimizer.step()

        loss = loss.data.cpu().detach().numpy()

        if classifier.regression:
            pbar.set_description('MSE_loss: %0.5f MAE_loss: %0.5f' % (loss, mae))
            total_loss.append(np.array([loss, mae]) * len(selected_idx))
        else:

            pbar.set_description('loss: %0.5f acc: %0.5f' % (loss, acc))
            total_loss.append(np.array([loss, acc]) * len(selected_idx))

        n_samples += len(selected_idx)
    if optimizer is None:
        assert n_samples == len(sample_idxes)
    total_loss = np.array(total_loss)
    avg_loss = np.sum(total_loss, 0) / n_samples
    all_scores = torch.cat(all_scores).cpu().numpy()

    if not classifier.regression:
        all_targets = np.array(all_targets)
        fpr, tpr, _ = metrics.roc_curve(all_targets, all_scores, pos_label=1)
        auc = metrics.auc(fpr, tpr)
        avg_loss = np.concatenate((avg_loss, [auc]))

    return avg_loss,Uw

def testloop_dataset(node_feat_new6,Dict,W,Uw,test_idxes,adj_one,g_list, adj_one_train,g_list_train,classifier, sample_idxes, optimizer=None, bsize=cmd_args.batch_size):
    total_loss = []
    total_iters = (len(sample_idxes) + (bsize - 1) * (optimizer is None)) // bsize
    pbar = tqdm(range(total_iters), unit='batch')
    all_targets = []
    all_scores = []

    n_samples = 0

    node_feat = node_feat_new6
    node_feat = torch.mm(node_feat, Dict)
    node_feat = torch.relu(node_feat)
    node_feat = node_feat.detach().numpy()

    z =node_feat


    kl_loss, q = Clustering(z, Uw,Dict)
    codes = np.argmax(q.detach().numpy(), 1)
    bin = np.bincount(codes)



    codestrain=[]
    for pos in pbar:
        selected_idx = sample_idxes[pos * bsize: (pos + 1) * bsize]
        batch_graph = [g_list[idx] for idx in selected_idx]
        targets = [g_list[idx].label for idx in selected_idx]
        all_targets += targets
        if classifier.regression:
            pred, mae, loss = classifier(g_list)
            all_scores.append(pred.cpu().detach())  # for binary classification
        else:

            logits, loss, acc = classifier(selected_idx,g_list,batch_graph,adj_one,node_feat1,q,Uw,bin,g_list_train,pos,codestrain)
            all_scores.append(logits[:, 1].cpu().detach())  # for binary classification

        if optimizer is not None:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        loss = loss.data.cpu().detach().numpy()

        if classifier.regression:
            pbar.set_description('MSE_loss: %0.5f MAE_loss: %0.5f' % (loss, mae))
            total_loss.append(np.array([loss, mae]) * len(selected_idx))
        else:

            pbar.set_description('loss: %0.5f acc: %0.5f' % (loss, acc))
            total_loss.append(np.array([loss, acc]) * len(selected_idx))

        n_samples += len(selected_idx)
    if optimizer is None:
        assert n_samples == len(sample_idxes)
    total_loss = np.array(total_loss)
    avg_loss = np.sum(total_loss, 0) / n_samples
    all_scores = torch.cat(all_scores).cpu().numpy()


    if not classifier.regression:
        all_targets = np.array(all_targets)
        fpr, tpr, _ = metrics.roc_curve(all_targets, all_scores, pos_label=1)
        auc = metrics.auc(fpr, tpr)
        avg_loss = np.concatenate((avg_loss, [auc]))

    return avg_loss
if __name__ == '__main__':
    print(cmd_args)
    random.seed(cmd_args.seed)
    np.random.seed(cmd_args.seed)
    torch.manual_seed(cmd_args.seed)

    train_graphs, test_graphs ,adj_one,adj_one_test,test_idxes_real= load_data()
    print('# train: %d, # test: %d' % (len(train_graphs), len(test_graphs)))

    if cmd_args.sortpooling_k <= 1:
        num_nodes_list = sorted([g.num_nodes for g in train_graphs + test_graphs])
        cmd_args.sortpooling_k = num_nodes_list[int(math.ceil(cmd_args.sortpooling_k * len(num_nodes_list))) - 1]
        cmd_args.sortpooling_k = max(10, cmd_args.sortpooling_k)
        print('k used in SortPooling is: ' + str(cmd_args.sortpooling_k))

    classifier = Classifier()
    if cmd_args.mode == 'gpu':
        classifier = classifier.cuda()
    num_centers = 100
    W = nn.Parameter(torch.zeros(size=(num_centers, num_centers)))
    nn.init.xavier_uniform_(W.data, gain=1.414)
    Uw = nn.Parameter(torch.zeros(size=(num_centers,38)))
    nn.init.xavier_uniform_(Uw.data, gain=1.414)
    Dict = nn.Parameter(torch.zeros(size=(38,80)))
    torch.nn.init.eye_(Dict.data)
    alpha = nn.Parameter(torch.ones(size=(1, 1)))

    nn.init.ones_(alpha.data)
    beta = nn.Parameter(torch.ones(size=(1, 1)))
    theta = nn.Parameter(torch.ones(size=(1, 1)))
    deta = nn.Parameter(torch.ones(size=(1, 1)))
    omg = nn.Parameter(torch.ones(size=(1, 1)))

    optimizer = optim.RMSprop([
        {'params': Dict, 'lr': 0.001},

                                {'params': Uw, 'lr': 0.02},
                                {'params': classifier.parameters(), 'lr': 0.001 },

                                ], lr=cmd_args.learning_rate)
    print("classifier.parameters()",classifier.parameters())

    train_idxes = list(range(len(train_graphs)))
    test_idxes = list(range(len(test_graphs)))

    best_loss = None

    t = Classifier()
    feature_label = t.PrepareFeatureLabel(train_graphs)
    feature_label_test = t.PrepareFeatureLabel(test_graphs)
    if len(feature_label) == 2:
        node_feat, labels = feature_label
        edge_feat = None
    elif len(feature_label) == 3:
        node_feat, edge_feat, labels = feature_label
    if len(feature_label_test) == 2:
        node_feat_test, labels_test = feature_label_test
        edge_feat = None
    elif len(feature_label_test) == 3:
        node_feat_test, edge_feat_test, labels_test = feature_label_test
    node_feat = np.concatenate((node_feat.cpu().numpy(), node_feat_test.cpu().numpy()), axis=0)
    count=0
    node_feat1 = node_feat
    node_feat1 = np.array(node_feat1)

    import pickle

    file = open('1order_LA_PTC.pkl', 'rb')
    LA1 = pickle.load(file)
    file = open('2order_LA_PTC.pkl', 'rb')
    LA2 = pickle.load(file)
    file = open('3order_LA_PTC.pkl', 'rb')
    LA3 = pickle.load(file)

    node_feat = torch.from_numpy(node_feat).type('torch.FloatTensor')
    LA1 = torch.from_numpy(LA1).type('torch.FloatTensor')
    LA2 = torch.from_numpy(LA2).type('torch.FloatTensor')
    LA3 = torch.from_numpy(LA3).type('torch.FloatTensor')
    for epoch in range(cmd_args.num_epochs):


        classifier.train()
        orderOne = 90 * (LA3)
        node_feat_Vec = torch.cat((node_feat, orderOne), 1)

        avg_loss,Uw= trainloop_dataset(node_feat_Vec,Dict,epoch,W,Uw,adj_one,train_graphs, adj_one_test,test_graphs,classifier, train_idxes, test_idxes,optimizer=optimizer,)
        if not cmd_args.printAUC:
            avg_loss[2] = 0.0
        print('\033[94maverage training of epoch %d: loss %.5f acc %.5f auc %.5f\033[0m' % (epoch, avg_loss[0], avg_loss[1], avg_loss[2]))

        classifier.eval()

        test_loss = testloop_dataset(node_feat_Vec[7896:8792],Dict,W,Uw,test_idxes_real,adj_one_test,test_graphs,adj_one,train_graphs, classifier, test_idxes,)
        if not cmd_args.printAUC:
            test_loss[2] = 0.0
        print('\033[95maverage test of epoch %d: loss %.5f acc %.5f auc %.5f\033[0m' % (epoch, test_loss[0], test_loss[1], test_loss[2]))


    if cmd_args.extract_features:
        features, labels = classifier.output_features(train_graphs)
        labels = labels.type('torch.FloatTensor')
        np.savetxt('extracted_features_train.txt',
                   torch.cat([labels.unsqueeze(1), features.cpu()], dim=1).detach().numpy(), '%.4f')
        features, labels = classifier.output_features(test_graphs)
        labels = labels.type('torch.FloatTensor')
        np.savetxt('extracted_features_test.txt',
                   torch.cat([labels.unsqueeze(1), features.cpu()], dim=1).detach().numpy(), '%.4f')
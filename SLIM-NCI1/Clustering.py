#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time : 2020/5/6 21:49
# @Author : Yaokang Zhu
# @Site :
# @File : Clustering.py
# @Software: PyCharm

import torch
import torch.nn.functional as F


def target_distribution(q):
    weight = q ** 2 / q.sum(0)
    return (weight.t() / weight.sum(1)).t()

def Clustering(z, Uw, Dict):
        # print("Uw", Uw.shape)
        # print("z", z.shape)
        # z = torch.from_numpy(z)
        # z = torch.mm(z, Dict)
        # Uw_mean = torch.mean(Uw)
        # z_mean = torch.mean(z)
        # z = (Uw_mean / z_mean) * z
        q = 1.0 / (1.0 + torch.sum(
            torch.pow(z.unsqueeze(1) - Uw, 2), 2))
        q = q.pow((1 + 1.0) / 2.0)
        q = (q.t() / torch.sum(q, 1)).t()
        """
         2.target distribution
       """
        p = target_distribution(q)

        """
       3.Kullback-Leibler (KL) divergence 
          """
        kl_loss = F.kl_div(q.log(), p)
        return kl_loss, q
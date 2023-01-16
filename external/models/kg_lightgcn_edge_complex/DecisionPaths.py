def warn(*args, **kwargs):
    pass


import warnings

warnings.warn = warn
from sklearn.tree import DecisionTreeClassifier
import random
import scipy
from scipy.sparse import csr_matrix
import os.path
import pandas as pd
from tqdm import tqdm
from sklearn.preprocessing import MultiLabelBinarizer
import torch
import numpy as np
from torch_sparse import SparseTensor
import multiprocessing as mp
mp.set_start_method('fork')
from collections import Counter

seed = 0


class DecisionPaths:
    def __init__(self, interactions, u_i_dict, kg, public_items, public_users, transaction, device, df_name, npr=10,
                 criterion='entropy'):
        self.interactions = interactions
        self.public_items = public_items
        self.public_users = public_users
        self.transaction = transaction
        self.u_i_dict = u_i_dict
        self.device = device
        self.npr = npr
        self.criterion = criterion
        self.dataset_name = df_name
        self._feature_to_private = None
        self.i_f = None
        self.train_dict = None
        self.edge_features = list()
        self.build_if(kg)
        self.build_decision_paths()  # for feature dev.

    def save_edge_features_df(self, edge_feature_df):
        name = 'decision_path' + str(self.npr) + "_" + str(self.criterion) + ".tsv"
        dataset_path = os.path.abspath(os.path.join('./data', self.dataset_name, 'kgtore', name))
        edge_feature_df.to_csv(dataset_path, sep='\t', header=False, index=False)

    def build_if(self, kg):
        i_f = kg
        i_f['subject'] = i_f['subject'].map(self.public_items)
        feature_column = i_f['predicate'].astype(str) + '-' + i_f['object'].astype(str)
        i_f = pd.concat([i_f['subject'], feature_column], axis=1)
        i_f.columns = ['subject', 'feature']
        self._feature_to_private = {f: i for i, f in enumerate(i_f['feature'].unique(), 1)}
        i_f['feature'] = i_f['feature'].map(self._feature_to_private)  # da df a mapping interno
        self.i_f = i_f.groupby('subject')['feature'].apply(set).to_dict()
        # self._data.i_train_dict = {u1: {i1: rating, i2:rating, ..}, u2: {ix:rating, ..}}

    def create_edge_features_matrix(self):
        edge_features = pd.DataFrame(self.edge_features)
        self.save_edge_features_df(edge_features)
        edge_features.columns = ['user', 'item', 'feature']
        edge_features['val'] = np.sign(edge_features['feature'])
        edge_features['feature'] = np.abs(edge_features['feature'])
        new_mapping = {p: pnew for pnew, p in enumerate(edge_features['feature'].unique())}
        edge_features['feature'] = edge_features['feature'].map(new_mapping)
        private_to_feature = {p: f for f, p in self._feature_to_private.items()}
        feature_to_private = {private_to_feature[p]: pnew for p, pnew in new_mapping.items()}
        # feature_to_private = {f: new_mapping[p] for f, p in self._feature_to_private.items()}
        self._feature_to_private = feature_to_private
        # reindex by interaction
        indices = edge_features.groupby(['user', 'item']).size().reset_index(name='Freq')
        index_list = [i for i in indices.index for z in range(indices.iloc[i, -1])]
        edge_features.index = index_list
        counted = Counter(index_list)
        val2 = [v for i, v in counted.items() for z in range(v)]
        edge_features['val'] = edge_features['val'] / val2
        self.edge_features = SparseTensor(row=torch.tensor(edge_features.index, dtype=torch.int64),
                                          col=torch.tensor(edge_features['feature'].astype(int).to_numpy(),
                                                           dtype=torch.int64),
                                          value=torch.tensor(edge_features['val'].astype(int).to_numpy(),
                                                             dtype=torch.int64),
                                          sparse_sizes=(self.transaction, edge_features['feature'].nunique())).to(
            self.device)

    def build_decision_paths(self):
        criterion = self.criterion
        items = set(self.i_f.keys())
        npr = self.npr

        print("Building decision trees")
        users = self.u_i_dict.keys()
        args = ((u, set(self.interactions[u].keys()), self.u_i_dict[u], items, self.i_f, npr, criterion) for u in users)
        n_procs = mp.cpu_count()-2
        print(f'Running multiprocessing with {n_procs} processes')

        with mp.Pool(n_procs) as pool:
            user_decision_paths = pool.starmap(user_decision_path, args)

        print("Building decision trees")

        self.edge_features = [p for path in user_decision_paths for p in path]
        self.create_edge_features_matrix()



def create_user_df(positive_items, negative_items, i_f, npr, random_seed=42):

    np.random.seed(random_seed)
    random.seed(random_seed)

    negatives_len = npr * len(positive_items)
    if len(positive_items) * npr <= len(negative_items):
        neg_items = random.sample(list(negative_items), k=negatives_len)
    else:
        ratio = len(negative_items) // len(positive_items)
        neg_items = random.sample(list(negative_items), k=ratio * len(positive_items)) if ratio > 0 else list(
            negative_items)
        neg_items.extend(random.choices(list(negative_items), k=negatives_len - len(neg_items)))

    all_items = list()
    all_items.extend(list(positive_items))
    all_items.extend(list(neg_items))
    mlb = MultiLabelBinarizer()
    d = {k: i_f[k] for k in all_items}
    df = pd.DataFrame(mlb.fit_transform(d.values()), columns=mlb.classes_)
    df['item_id'] = d.keys()
    df['positive'] = df['item_id'].isin(positive_items).astype(int)
    return df

def create_user_tree(df, npr, criterion):
    clf = DecisionTreeClassifier(criterion=criterion, class_weight={1: npr, 0: 1}, random_state=seed)
    X = csr_matrix(df.iloc[:, :-2].values)
    y = df.iloc[:, -1].values
    clf.fit(X, y)
    return clf


def retrieve_decision_paths(df, clf, u, user_i_dict):
    full_positive_df = df.iloc[pd.Index(df['item_id']).get_indexer(user_i_dict)]
    csr = scipy.sparse.csr_matrix(full_positive_df.iloc[:, :-2].values)
    decision_path = clf.decision_path(csr)
    u_dp = list()
    for i in range(0, full_positive_df.shape[0]):
        sample_no = i
        dp_i = decision_path.indices[decision_path.indptr[sample_no]: decision_path.indptr[sample_no + 1]]
        a = clf.tree_.feature[dp_i][clf.tree_.feature[dp_i] != -2]
        feature_is_present = full_positive_df.iloc[sample_no, a]
        feature_is_present = feature_is_present.replace(0, -1)
        final_dp_feature = list(feature_is_present.index.astype(int) * feature_is_present)
        u_dp.extend([[u, full_positive_df.iloc[sample_no, -2], j] for j in
                     final_dp_feature])
    return u_dp


def user_decision_path(user, user_items, user_i_dict, items: set, item_features: dict, npr, criterion):
    df = create_user_df(user_items, set.difference(items, user_items), item_features, npr)
    clf = create_user_tree(df, npr, criterion)
    u_dp = retrieve_decision_paths(df, clf, user, user_i_dict)
    return u_dp

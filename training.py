import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import torch


def run_selftrain_GC(clients, server, local_epoch):
    # all clients are initialized with the same weights
    for client in clients:
        client.download_from_server(server)

    allAccs = {}
    for client in clients:
        client.local_train(local_epoch)

        loss, acc = client.evaluate()
        allAccs[client.name] = [client.train_stats['trainingAccs'][-1], client.train_stats['valAccs'][-1], acc]
        print("  > {} done.".format(client.name))

    return allAccs


def run_fedavg(clients, server, COMMUNICATION_ROUNDS, local_epoch, samp=None, frac=1.0):
    for client in clients:
        client.download_from_server(server)

    if samp is None:
        sampling_fn = server.randomSample_clients
        frac = 1.0

    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")

        if c_round == 1:
            selected_clients = clients
        else:
            selected_clients = sampling_fn(clients, frac)

        for client in selected_clients:
            # only get weights of graphconv layers
            client.local_train(local_epoch)

        server.aggregate_weights(selected_clients)
        for client in selected_clients:
            client.download_from_server(server)

    frame = pd.DataFrame()
    for client in clients:
        loss, acc = client.evaluate()
        frame.loc[client.name, 'test_acc'] = acc

    def highlight_max(s):
        is_max = s == s.max()
        return ['background-color: yellow' if v else '' for v in is_max]

    fs = frame.style.apply(highlight_max).data
    print(fs)
    return frame


def run_fedprox(clients, server, COMMUNICATION_ROUNDS, local_epoch, mu, samp=None, frac=1.0):
    for client in clients:
        client.download_from_server(server)

    if samp is None:
        sampling_fn = server.randomSample_clients
        frac = 1.0
    if samp == 'random':
        sampling_fn = server.randomSample_clients

    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")

        if c_round == 1:
            selected_clients = clients
        else:
            selected_clients = sampling_fn(clients, frac)

        for client in selected_clients:
            client.local_train_prox(local_epoch, mu)

        server.aggregate_weights(selected_clients)
        for client in selected_clients:
            client.download_from_server(server)

            # cache the aggregated weights for next round
            client.cache_weights()

    frame = pd.DataFrame()
    for client in clients:
        loss, acc = client.evaluate()
        frame.loc[client.name, 'test_acc'] = acc

    def highlight_max(s):
        is_max = s == s.max()
        return ['background-color: yellow' if v else '' for v in is_max]

    fs = frame.style.apply(highlight_max).data
    print(fs)
    return frame


def run_gcfl(clients, server, COMMUNICATION_ROUNDS, local_epoch, EPS_1, EPS_2):
    cluster_indices = [np.arange(len(clients)).astype("int")]
    client_clusters = [[clients[i] for i in idcs] for idcs in cluster_indices]

    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")
        if c_round == 1:
            for client in clients:
                client.download_from_server(server)

        participating_clients = server.randomSample_clients(clients, frac=1.0)

        for client in participating_clients:
            client.compute_weight_update(local_epoch)
            client.reset()

        similarities = server.compute_pairwise_similarities(clients)

        cluster_indices_new = []
        for idc in cluster_indices:
            max_norm = server.compute_max_update_norm([clients[i] for i in idc])
            mean_norm = server.compute_mean_update_norm([clients[i] for i in idc])
            if mean_norm < EPS_1 and max_norm > EPS_2 and len(idc) > 2 and c_round > 20:
                server.cache_model(idc, clients[idc[0]].W, acc_clients)
                c1, c2 = server.min_cut(similarities[idc][:, idc], idc)
                cluster_indices_new += [c1, c2]
            else:
                cluster_indices_new += [idc]

        cluster_indices = cluster_indices_new
        client_clusters = [[clients[i] for i in idcs] for idcs in cluster_indices]  # initial: [[0, 1, ...]]

        server.aggregate_clusterwise(client_clusters)

        acc_clients = [client.evaluate()[1] for client in clients]

    for idc in cluster_indices:
        server.cache_model(idc, clients[idc[0]].W, acc_clients)

    results = np.zeros([len(clients), len(server.model_cache)])
    for i, (idcs, W, accs) in enumerate(server.model_cache):
        results[idcs, i] = np.array(accs)

    frame = pd.DataFrame(results, columns=["FL Model"] + ["Model {}".format(i)
                                                          for i in range(results.shape[1] - 1)],
                         index=["{}".format(clients[i].name) for i in range(results.shape[0])])
    frame = pd.DataFrame(frame.max(axis=1))
    frame.columns = ['test_acc']
    print(frame)

    return frame


def run_gcflplus(clients, server, COMMUNICATION_ROUNDS, local_epoch, EPS_1, EPS_2, seq_length, standardize):
    cluster_indices = [np.arange(len(clients)).astype("int")]
    client_clusters = [[clients[i] for i in idcs] for idcs in cluster_indices]

    seqs_grads = {c.id:[] for c in clients}
    for client in clients:
        client.download_from_server(server)

    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")
        if c_round == 1:
            for client in clients:
                client.download_from_server(server)

        participating_clients = server.randomSample_clients(clients, frac=1.0)

        for client in participating_clients:
            client.compute_weight_update(local_epoch)
            client.reset()

            seqs_grads[client.id].append(client.convGradsNorm)

        cluster_indices_new = []
        for idc in cluster_indices:
            max_norm = server.compute_max_update_norm([clients[i] for i in idc])
            mean_norm = server.compute_mean_update_norm([clients[i] for i in idc])
            if mean_norm < EPS_1 and max_norm > EPS_2 and len(idc) > 2 and c_round > 20 \
                    and all(len(value) >= seq_length for value in seqs_grads.values()):

                server.cache_model(idc, clients[idc[0]].W, acc_clients)

                tmp = [seqs_grads[id][-seq_length:] for id in idc]
                dtw_distances = server.compute_pairwise_distances(tmp, standardize)
                c1, c2 = server.min_cut(np.max(dtw_distances)-dtw_distances, idc)
                cluster_indices_new += [c1, c2]

                seqs_grads = {c.id: [] for c in clients}
            else:
                cluster_indices_new += [idc]

        cluster_indices = cluster_indices_new
        client_clusters = [[clients[i] for i in idcs] for idcs in cluster_indices]

        server.aggregate_clusterwise(client_clusters)

        acc_clients = [client.evaluate()[1] for client in clients]

    for idc in cluster_indices:
        server.cache_model(idc, clients[idc[0]].W, acc_clients)

    results = np.zeros([len(clients), len(server.model_cache)])
    for i, (idcs, W, accs) in enumerate(server.model_cache):
        results[idcs, i] = np.array(accs)

    frame = pd.DataFrame(results, columns=["FL Model"] + ["Model {}".format(i)
                                                          for i in range(results.shape[1] - 1)],
                         index=["{}".format(clients[i].name) for i in range(results.shape[0])])

    frame = pd.DataFrame(frame.max(axis=1))
    frame.columns = ['test_acc']
    print(frame)

    return frame


def run_gcflplus_dWs(clients, server, COMMUNICATION_ROUNDS, local_epoch, EPS_1, EPS_2, seq_length, standardize):
    cluster_indices = [np.arange(len(clients)).astype("int")]
    client_clusters = [[clients[i] for i in idcs] for idcs in cluster_indices]

    seqs_grads = {c.id:[] for c in clients}
    for client in clients:
        client.download_from_server(server)

    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")
        if c_round == 1:
            for client in clients:
                client.download_from_server(server)

        participating_clients = server.randomSample_clients(clients, frac=1.0)

        for client in participating_clients:
            client.compute_weight_update(local_epoch)
            client.reset()

            seqs_grads[client.id].append(client.convDWsNorm)

        cluster_indices_new = []
        for idc in cluster_indices:
            max_norm = server.compute_max_update_norm([clients[i] for i in idc])
            mean_norm = server.compute_mean_update_norm([clients[i] for i in idc])
            if mean_norm < EPS_1 and max_norm > EPS_2 and len(idc) > 2 and c_round > 20 \
                    and all(len(value) >= seq_length for value in seqs_grads.values()):

                server.cache_model(idc, clients[idc[0]].W, acc_clients)

                tmp = [seqs_grads[id][-seq_length:] for id in idc]
                dtw_distances = server.compute_pairwise_distances(tmp, standardize)
                c1, c2 = server.min_cut(np.max(dtw_distances)-dtw_distances, idc)
                cluster_indices_new += [c1, c2]

                seqs_grads = {c.id: [] for c in clients}
            else:
                cluster_indices_new += [idc]

        cluster_indices = cluster_indices_new
        client_clusters = [[clients[i] for i in idcs] for idcs in cluster_indices]

        server.aggregate_clusterwise(client_clusters)

        acc_clients = [client.evaluate()[1] for client in clients]

    for idc in cluster_indices:
        server.cache_model(idc, clients[idc[0]].W, acc_clients)

    results = np.zeros([len(clients), len(server.model_cache)])
    for i, (idcs, W, accs) in enumerate(server.model_cache):
        results[idcs, i] = np.array(accs)

    frame = pd.DataFrame(results, columns=["FL Model"] + ["Model {}".format(i)
                                                          for i in range(results.shape[1] - 1)],
                         index=["{}".format(clients[i].name) for i in range(results.shape[0])])
    frame = pd.DataFrame(frame.max(axis=1))
    frame.columns = ['test_acc']
    print(frame)

    return frame
def run_prototype(clients, server, COMMUNICATION_ROUNDS, local_epoch, samp=None, frac=1.0):
    
    l = []
    selected_clients = clients
    for client in selected_clients:
        client.motif_construction()
        
    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        l1 = 0
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")
        

        if c_round == 1:
            for client in selected_clients:
                
                client.prototype_update()
        server.aggregate_prototype(selected_clients)
        

        for client in selected_clients:
            client.download_code(server)
            client.prototype_train(server)
            loss, acc = client.evaluate()
            l1 += loss
        l.append(l1 / len(clients))

        

        for client in selected_clients:
            client.clear_prototype()
        server.clear_prototype()
    l = np.array(l)
    np.save('FG.npy', l)
    frame = pd.DataFrame()
    for client in clients:
        loss, acc = client.evaluate()
        frame.loc[client.name, 'test_acc'] = acc

    def highlight_max(s):
        is_max = s == s.max()
        return ['background-color: yellow' if v else '' for v in is_max]

    fs = frame.style.apply(highlight_max).data
    print(fs)
    print('w/o reput')
    return frame



def run_protoreput(clients, server, COMMUNICATION_ROUNDS, device, samp=None, frac=1.0):
    selected_clients = clients
    rs = torch.zeros(len(clients))
    for i in range(len(clients)):
        rs[i] = 1 / len(clients)

    for client in selected_clients:
        client.motif_construction()
        
    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")
        if c_round == 1:
            for client in selected_clients:
                
                client.prototype_update()
        


        
        







        server.reput_aggregate_prototype(rs, selected_clients)
        phis = torch.zeros(len(clients))
        for i, client in enumerate(selected_clients):
            phis[i] = client.cosine_similar(server)
            
        rs = 0.95 * rs + 0.05 * phis
        rs = torch.clamp(rs, min=1e-3)
        rs = torch.div(rs, rs.sum())

        for client in selected_clients:
            client.download_code(server)
            client.prototype_train(server)

        

        for client in selected_clients:
            client.clear_prototype()
        server.clear_prototype()
        
        #torch.cuda.empty_cache()

    frame = pd.DataFrame()
    for client in clients:
        loss, acc = client.evaluate()
        frame.loc[client.name, 'test_acc'] = acc

    def highlight_max(s):
        is_max = s == s.max()
        return ['background-color: yellow' if v else '' for v in is_max]

    fs = frame.style.apply(highlight_max).data
    print(fs)
    print('reput')
    return frame
        
        
    
    
    


def run_protoreput2(clients, server, COMMUNICATION_ROUNDS, device, samp=None, frac=1.0):
    selected_clients = clients
    rs = torch.zeros(len(clients))
    for i in range(len(clients)):
        rs[i] = 1 / len(clients)

    for client in selected_clients:
        client.motif_construction()
        
    for c_round in range(1, COMMUNICATION_ROUNDS + 1):
        if (c_round) % 50 == 0:
            print(f"  > round {c_round}")
        if c_round == 1:
            for client in selected_clients:
                
                client.prototype_update()
        if c_round == 1:
            server.aggregate_prototype(selected_clients)
        else:
            server.reput_aggregate_prototype2(rs, selected_clients)

        


        
        







        #server.reput_aggregate_prototype(rs, selected_clients)
        phis = torch.zeros(len(clients))
        for i, client in enumerate(selected_clients):
            phis[i] = client.cosine_similar(server)
            
        rs = 0.95 * rs + 0.05 * phis
        rs = torch.clamp(rs, min=1e-3)
        rs = torch.div(rs, rs.sum())

        for client in selected_clients:
            client.download_code(server)
            client.prototype_train(server)
            

        
        server.update_reput(selected_clients)
        
        for client in selected_clients:
            client.clear_prototype()
        server.clear_prototype()
        
        #torch.cuda.empty_cache()

    frame = pd.DataFrame()
    for client in clients:
        loss, acc = client.evaluate()
        frame.loc[client.name, 'test_acc'] = acc

    def highlight_max(s):
        is_max = s == s.max()
        return ['background-color: yellow' if v else '' for v in is_max]

    fs = frame.style.apply(highlight_max).data
    print(fs)
    print('reput2')
    return frame
        

    

    

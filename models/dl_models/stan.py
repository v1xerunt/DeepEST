
import torch
import torch.nn as nn
import torch.nn.functional as F


class GATLayer(nn.Module):
    def __init__(self, g, input_size, out_dim):
        super(GATLayer, self).__init__()
        self.g = g
        self.fc = nn.Linear(input_size, out_dim)
        self.attn_fc = nn.Linear(2 * out_dim, 1)
        self.reset_parameters()

    def reset_parameters(self):
        gain = nn.init.calculate_gain('relu')
        nn.init.xavier_normal_(self.fc.weight, gain=gain)
        nn.init.xavier_normal_(self.attn_fc.weight, gain=gain)

    def edge_attention(self, edges):
        z2 = torch.cat([edges.src['z'], edges.dst['z']], dim=1)
        a = self.attn_fc(z2)
        return {'e': F.leaky_relu(a)}

    def message_func(self, edges):
        return {'z': edges.src['z'], 'e': edges.data['e']}

    def reduce_func(self, nodes):
        alpha = F.softmax(nodes.mailbox['e'], dim=1)
        h = torch.sum(alpha * nodes.mailbox['z'], dim=1)
        return {'h': h}

    def forward(self, h):
        z = self.fc(h)
        self.g.ndata['z'] = z
        self.g.apply_edges(self.edge_attention)
        self.g.update_all(self.message_func, self.reduce_func)
        return self.g.ndata.pop('h')


class MultiHeadGATLayer(nn.Module):
    def __init__(self, g, input_size, out_dim, num_heads, merge='cat'):
        super(MultiHeadGATLayer, self).__init__()
        self.heads = nn.ModuleList()
        for i in range(num_heads):
            self.heads.append(GATLayer(g, input_size, out_dim))
        self.merge = merge

    def forward(self, h):
        b = h.shape[0]
        outs = []
        for i in range(b):
            h_i = h[i]
            head_outs = [attn_head(h_i) for attn_head in self.heads]
            if self.merge == 'cat':
                outs.append(torch.cat(head_outs, dim=1))
            else:
                outs.append(torch.mean(torch.stack(head_outs)))
        return torch.stack(outs)


class STAN(nn.Module):
    def __init__(
            self,
            g,
            input_size: int,
            pred_window: int,
            hidden_dim1: int = 64,
            hidden_dim2: int = 64,
            gru_dim: int = 64,
            num_heads: int = 1,
            device: str = "cpu"):
        super(STAN, self).__init__()
        self.g = g

        self.layer1 = MultiHeadGATLayer(self.g, input_size, hidden_dim1, num_heads)
        self.layer2 = MultiHeadGATLayer(self.g, hidden_dim1 * num_heads, hidden_dim2, 1)

        self.pred_window = pred_window
        self.gru = gru = nn.GRU(input_size=hidden_dim2, hidden_size=gru_dim, batch_first=True)

        self.hidden_dim2 = hidden_dim2
        self.gru_dim = gru_dim
        self.device = device
        self.pred = nn.Linear(gru_dim, pred_window)

    def forward(self, dynamic, h=None):
        batch, num_loc, timestep, n_feat = dynamic.size()

        if h is None:
            h = torch.zeros(batch, self.gru_dim).to(self.device)
            gain = nn.init.calculate_gain('relu')
            nn.init.xavier_normal_(h, gain=gain)
            h = h.unsqueeze(0)
        gat_outs_list = []
        for each_step in range(timestep):
            cur_h = self.layer1(dynamic[:, :, each_step, :])
            cur_h = F.elu(cur_h)
            cur_h = self.layer2(cur_h)
            cur_h = F.elu(cur_h)
            gat_outs_list.append(cur_h)
        gat_outs = torch.stack(gat_outs_list, dim=1)
        output_list = []
        for i in range(num_loc):
            gat_out = gat_outs[:, :, i, :]
            _, h = self.gru(gat_out, h)
            output_list.append(self.pred(h))
        outputs = torch.stack(output_list).squeeze(1).permute(1, 0, 2)
        return outputs
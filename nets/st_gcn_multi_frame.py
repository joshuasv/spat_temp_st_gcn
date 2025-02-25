import torch
import torch.nn as nn


from nets.utils.tgcn import ConvTemporalGraphical
from nets.utils.graph_frames import Graph
from nets.utils.graph_frames_withpool_2 import Graph_pool
from nets.non_local_embedded_gaussian import NONLocalBlock2D


inter_channels = [128, 128, 256]

fc_out = inter_channels[-1]
fc_unit = 512
class Model(nn.Module):
    """

    Args:
        in_channels (int): Number of channels in the input data
        cat: True: concatinate coarse and fine features
            False: add coarse and fine features

    Shape:
        - Input: :math:`(N, in_channels, T_{in}, V_{in}, M_{in})`
            :math:`N` is a batch size,
            :math:`T_{in}` is a length of input sequence,
            :math:`V_{in}` is the number of graph nodes for each frame,
            :math:`M_{in}` is the number of instance in a frame. (In this task always equals to 1)
    Return:
        out_all_frame: True: return all frames 3D results
                        False: return target frame result

        x_out: final output.

    """

    def __init__(self, opt):
        super().__init__()

        # load graph
        self.momentum =  0.1
        self.in_channels = opt.in_channels
        self.out_channels = opt.out_channels
        self.layout = opt.layout
        self.strategy = opt.strategy
        self.cat = True
        self.inplace = True
        self.pad = opt.pad

        # original graph
        self.graph = Graph(self.layout, self.strategy, pad=opt.pad)
        # get adjacency matrix of K clusters
        self.A = torch.tensor(self.graph.A, dtype=torch.float32, requires_grad=False).cuda() # K, T*V, T*V

        # pooled graph
        self.graph_pool = Graph_pool(self.layout, self.strategy, pad=opt.pad)
        self.A_pool = torch.tensor(self.graph_pool.A, dtype=torch.float32, requires_grad=False).cuda()



        # build networks
        kernel_size = self.A.size(0)
        kernel_size_pool = self.A_pool.size(0)

        self.data_bn = nn.BatchNorm1d(self.in_channels * self.graph.num_node_each, self.momentum)

        self.st_gcn_networks = nn.ModuleList((
            st_gcn(self.in_channels, inter_channels[0], kernel_size, residual=False),
            st_gcn(inter_channels[0], inter_channels[1], kernel_size),
            st_gcn(inter_channels[1], inter_channels[2], kernel_size),
        ))


        self.st_gcn_pool = nn.ModuleList((
            st_gcn(inter_channels[-1], fc_unit, kernel_size_pool),
            st_gcn(fc_unit, fc_unit,kernel_size_pool),
        ))


        self.conv4 = nn.Sequential(
            nn.Conv2d(fc_unit, fc_unit, kernel_size=(3, 1), padding = (1, 0)),
            nn.BatchNorm2d(fc_unit, momentum=self.momentum),
            nn.ReLU(inplace=self.inplace),
            nn.Dropout(0.25)
        )

        self.conv2 = nn.Sequential(
            nn.Conv2d(fc_unit*2, fc_out, kernel_size=(1, 1), padding = (0, 0)),
            nn.BatchNorm2d(fc_out, momentum=self.momentum),
            nn.ReLU(inplace=self.inplace),
            nn.Dropout(0.1)
        )

        self.non_local = NONLocalBlock2D(in_channels=fc_out*2, sub_sample=False)

        # fcn for final layer prediction
        fc_in = inter_channels[-1]+fc_out if self.cat else inter_channels[-1]
        self.fcn = nn.Sequential(
            nn.Dropout(0.1, inplace=True),
            nn.Conv2d(fc_in, self.out_channels, kernel_size=1)
        )


    # Max pooling of size p. Must be a power of 2.
    def graph_max_pool(self, x, p,stride=None):
        if max(p) > 1:
            if stride is None:
                x = nn.MaxPool2d(p)(x)  # B x F x V/p
            else:
                x = nn.MaxPool2d(kernel_size=p,stride=stride)(x)  # B x F x V/p
            return x
        else:
            return x


    def forward(self, x,out_all_frame=False):

        # data normalization
        N, C, T, V, M = x.size()

        x = x.permute(0, 4, 3, 1, 2).contiguous() # N, M, V, C, T
        x = x.view(N * M, V * C, T)

        x = self.data_bn(x)
        x = x.view(N, M, V, C, T)
        x = x.permute(0, 1, 3, 4, 2).contiguous()
        x = x.view(N * M, C, 1, -1) #(N * M), C, 1, (T*V)

        # forwad GCN
        gcn_list = list(self.st_gcn_networks)
        for i_gcn, gcn in enumerate(gcn_list):
            x, _ = gcn(x, self.A) # (N * M), C, 1, (T*V)

        x = x.view(N, -1, T, V) # N, C, T ,V

        # Pooling
        for i in range(len(self.graph.part)):
            num_node= len(self.graph.part[i])
            x_i = x[:, :, :, self.graph.part[i]]
            x_i = self.graph_max_pool(x_i, (1, num_node))
            x_sub1 = torch.cat((x_sub1, x_i), -1) if i > 0 else x_i # Final to N, C, T, (NUM_SUB_PARTS)

        x_sub1, _ = self.st_gcn_pool[0](x_sub1.view(N, -1, 1, T*len(self.graph.part)), self.A_pool.clone())  # N, 512, 1, (T*NUM_SUB_PARTS)
        x_sub1, _ = self.st_gcn_pool[1](x_sub1, self.A_pool.clone())  # N, 512, 1, (T*NUM_SUB_PARTS)
        x_sub1 = x_sub1.view(N, -1, T, len(self.graph.part))

        x_pool_1 = self.graph_max_pool(x_sub1, (1, len(self.graph.part)))  # N, 512, T, 1
        x_pool_1 = self.conv4(x_pool_1)# N, C, T, 1

        x_up_sub = torch.cat((x_pool_1.repeat(1, 1, 1, len(self.graph.part)), x_sub1), 1)  # N, 1024, T, 5
        x_up_sub = self.conv2(x_up_sub) #N, C, T, 5


        # upsample
        x_up = torch.zeros((N * M, fc_out, T, V)).cuda()
        for i in range(len(self.graph.part)):
            num_node = len(self.graph.part[i])
            x_up[:, :, :, self.graph.part[i]] = x_up_sub[:, :, :, i].unsqueeze(-1).repeat(1, 1, 1, num_node)


        #for non-local and fcn
        x = torch.cat((x,x_up),1)
        x = self.non_local(x)  # N, 2C, T, V
        x = self.fcn(x) # N, 3, T, V

        # output
        x = x.view(N, M, -1, T, V).permute(0, 2, 3, 4, 1).contiguous() # N, C, T, V, M
        if out_all_frame:
            x_out = x
        else:
            x_out= x[:, :, self.pad].unsqueeze(2)
        return x_out


class st_gcn(nn.Module):
    """Applies a spatial temporal graph convolution over an input graph sequence.

    Args:
        in_channels (int): Number of channels in the input sequence data
        out_channels (int): Number of channels produced by the convolution
        kernel_size :number of the node clusters

        dropout (int, optional): Dropout rate of the final output. Default: 0
        residual (bool, optional): If ``True``, applies a residual mechanism. Default: ``True``

    Shape:
        - Input[0]: Input graph sequence in :math:`(N, in_channels, 1, T*V)` format
        - Input[1]: Input graph adjacency matrix in :math:`(K, T*V, T*V)` format
        - Output[0]: Outpu graph sequence in :math:`(N, out_channels, 1, T*V)` format
        - Output[1]: Graph adjacency matrix for output data in :math:`(K, T*V, T*V)` format

        where
            :math:`N` is a batch size,
            :math:`K` is the kernel size
            :math:`T` is a length of sequence,
            :math:`V` is the number of graph nodes of each frame.

    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride=1,
                 dropout=0.05,
                 residual=True):

        super().__init__()
        self.inplace = True

        self.momentum = 0.1
        self.gcn = ConvTemporalGraphical(in_channels, out_channels, kernel_size)

        self.tcn = nn.Sequential(

            nn.BatchNorm2d(out_channels, momentum=self.momentum),
            nn.ReLU(inplace=self.inplace),
            nn.Dropout(0.05),
            nn.Conv2d(
                out_channels,
                out_channels,
                (1, 1),
                (stride, 1),
                padding = 0,
            ),
            nn.BatchNorm2d(out_channels, momentum=self.momentum),
            nn.Dropout(dropout, inplace=self.inplace),


        )

        if not residual:
            self.residual = lambda x: 0

        elif (in_channels == out_channels) and (stride == 1):
            self.residual = lambda x: x

        else:
            self.residual = nn.Sequential(
                nn.Conv2d(
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    stride=(stride, 1)),
                nn.BatchNorm2d(out_channels, momentum=self.momentum),
            )

        self.relu = nn.ReLU(inplace=self.inplace)

    def forward(self, x, A):

        res = self.residual(x)
        x, A = self.gcn(x, A)

        x = self.tcn(x) + res

        return self.relu(x), A

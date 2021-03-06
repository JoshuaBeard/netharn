"""
Reproduces RegionLoss from Darknet:
    https://github.com/pjreddie/darknet/blob/master/src/region_layer.c

Based off RegionLoss from Lightnet:
    https://gitlab.com/EAVISE/lightnet/blob/master/lightnet/network/loss/_regionloss.py

Speedups
    [ ] - Preinitialize anchor tensors


PJReddie's delta formulation:

    # NOTES:
        output coordinates -
           x and y are offsets from anchor centers in Wout,Hout space.
           w and h are in 01 coordinates


        truth.{x, y, w, h} - true box in 01 coordinates
        tx, ty, tw, th - true box in output coordinates

    # Transform output coordinates to bbox pred
    box get_region_box(float *x, float *biases, int n, int index, int i, int j,
                       int w, int h, int stride)
    {
        # bbox pred is in 0 - 1 coordinates
        box b;
        b.x = (i + x[index + 0*stride]) / w;
        b.y = (j + x[index + 1*stride]) / h;
        b.w = exp(x[index + 2*stride]) * biases[2*n]   / w;
        b.h = exp(x[index + 3*stride]) * biases[2*n+1] / h;
        return b;
    }


    # VOC Config:
    # https://github.com/pjreddie/darknet/blob/master/cfg/yolov2-voc.cfg

    # WHEN SEEN < 128000 CASE
    if(*(net.seen) < 12800){
        box truth = {0};
        truth.x = (i + .5)/l.w;
        truth.y = (j + .5)/l.h;
        truth.w = l.biases[2*n]/l.w;    # l.biases are anchor boxes
        truth.h = l.biases[2*n+1]/l.h;
        delta_region_box(
            truth, l.output, l.biases, n, box_index, i, j, l.w, l.h,
            delta=l.delta, scale=.01, stride=l.w*l.h);
    }

    # COORDINATE LOSS
    # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L254
    # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L86
    # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L293
    float iou = delta_region_box(
        truth, l.output, l.biases,
        n=best_n, index=box_index, i=i, j=j, w=l.w, h=l.h, delta=l.delta,
        scale=l.coord_scale * (2 - truth.w*truth.h), stride=l.w*l.h);
    {
        # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L86

        # CONVERT THE TRUTH BOX INTO OUTPUT COORD SPACE
        float tx = (truth.x * l.w - i);
        float ty = (truth.y * l.h - j);
        float tw = log(truth.w * l.w / biases[2*n]);
        float th = log(truth.h * l.h / biases[2*n + 1]);

        delta[index + 0*stride] = scale * (tx - x[index + 0*stride]);
        delta[index + 1*stride] = scale * (ty - x[index + 1*stride]);
        delta[index + 2*stride] = scale * (tw - x[index + 2*stride]);
        delta[index + 3*stride] = scale * (th - x[index + 3*stride]);
    }


    # CLASSIFICATION LOSS
    # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L112
    # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L314
    delta_region_class(
        output=l.output, delta=l.delta, index=class_index, class=class,
        classes=l.classes, hier=l.softmax_tree, scale=l.class_scale,
        stride=l.w*l.h, avg_cat=&avg_cat, tag=!l.softmax);


    # OBJECTNESS LOSS: FOREGROUND
    # THIS IS THE DEFAULT IOU LOSS FOR UNMATCHED OBJECTS
    l.delta[obj_index] = l.noobject_scale * (0 - l.output[obj_index]);
    if(l.background) {
        # BG is 0
        l.delta[obj_index] = l.noobject_scale * (1 - l.output[obj_index]);
    }
    if (best_iou > l.thresh) {
        l.delta[obj_index] = 0;
    }


    # OBJECTNESS LOSS: BACKGROUND
    l.delta[obj_index] = l.object_scale * (1 - l.output[obj_index]);
    # rescore is 1 for VOC
    if (l.rescore) {
        l.delta[obj_index] = l.object_scale * (iou - l.output[obj_index]);
    }
    # background defaults to 0
    if(l.background){
        l.delta[obj_index] = l.object_scale * (0 - l.output[obj_index]);
    }


    IOU computation equivalent to:
        * default all conf_mask to noobject_scale
        * default all tconf to 0
        * for any predictions with a best iou over a threshold (0.6)
            - set its mask to 0 (equivalent to setting its delta to 0)
        * for any matching pred / true positions:
            - switch conf_mask to object_scale
            - switch tconf to (iou if rescore else 1)

Summary:
    Coordinate Loss:
        loss = coord_scale * (m * t - m * p) ** 2

        * When seen < N, YOLO sets the scale to 0.01
            - In our formulation, set m=sqrt(0.01 / coord_scale)
"""

import math
import torch
import torch.nn as nn
import numpy as np  # NOQA
# import functools
from torch.autograd import Variable
from netharn import util
from netharn.util import profiler

__all__ = ['RegionLoss']


def sympy_check_coordinate_loss():
    import sympy
    # s = coord_scale
    # m = coord_mask
    # p = coords
    # t = tcoords
    # tw, th = true width and height in normalized 01 coordinates
    s, m, p, t, tw, th = sympy.symbols('s, m, p, t, tw, th')

    # This is the general MSE loss forumlation for box coordinates We will
    # always use this at the end of our computation (so we can efficiently
    # broadcast the tensor). s, t, and p are always fixed constants.
    # The main questsion is: how do we set m to match PJR's results?
    loss_box = .5 * s * (m * t - m * p) ** 2

    # Our formulation produces this negative derivative
    our_grad = -1 * sympy.diff(loss_box, p)
    print('our_grad = {!r}'.format(our_grad))

    # PJReddie hard codes this negative derivative
    pjr_grad = s * (t - p)

    # Check how they differ
    # (see what the mask must be assuming the scale is the same)
    eq = sympy.Eq(pjr_grad, our_grad)
    sympy.solve(eq)

    # However, the scale is not the same in all cases PJReddie will change it
    # Depending on certain cases.

    # Coordinate Loss:
    #     loss = coord_scale * (m * t - m * p) ** 2

    #
    # BACKGROUND LOW SEEN CASE:
    # --------------
    #   * When seen < N, YOLO sets the scale to 0.01
    #       - In our formulation, set m=sqrt(0.01 / coord_scale)
    #
    #  * NOTE this loss is only applied to background predictions,
    #       it is overridden if a a true object is assigned to the box
    #       this is only a default case.
    bgseen_pjr_grad = pjr_grad.subs({s: 0.01})
    eq = sympy.Eq(bgseen_pjr_grad, our_grad)
    bgseen_m_soln = sympy.solve(eq, m)[1]
    print('when seen < N, set m = {}'.format(bgseen_m_soln))
    # Check that this is what we expect
    bgseen_m_expected = sympy.sqrt(0.01 / s)
    assert (bgseen_m_soln - bgseen_m_expected) == 0

    #
    # BACKGROUND LOW SEEN CASE:
    #     * when seen is high, the bbox loss in the background is just 0, we
    #     can achive this by simply setting m = 0
    assert our_grad.subs({'m': 0}) == 0

    #
    # FORGROUND NORMAL CASE
    # -----------
    # Whenever a pred box is assigned to a true object it gets this coordinate
    # loss

    # In the normal case pjr sets
    # true box width and height
    norm_pjr_grad = pjr_grad.subs({s: s * (2 - tw * th)})
    eq = sympy.Eq(norm_pjr_grad, our_grad)
    fgnorm_m_soln = sympy.solve(eq, m)[1]
    print('fgnorm_m_soln = {!r}'.format(fgnorm_m_soln))
    # Check that this is what we expect
    fgnorm_m_expected = sympy.sqrt(2.0 - th * tw)
    assert (fgnorm_m_soln - fgnorm_m_expected) == 0


class BaseLossWithCudaState(torch.nn.modules.loss._Loss):
    """
    Helper to keep track of if a loss module is in cpu or gpu mod
    """
    def __init__(self):
        super(BaseLossWithCudaState, self).__init__()
        self._iscuda = False
        self._device_num = None

    def cuda(self, device_num=None, **kwargs):
        self._iscuda = True
        self._device_num = device_num
        return super(BaseLossWithCudaState, self).cuda(device_num, **kwargs)

    def cpu(self):
        self._iscuda = False
        self._device_num = None
        return super(BaseLossWithCudaState, self).cpu()

    @property
    def is_cuda(self):
        return self._iscuda

    def get_device(self):
        if self._device_num is None:
            return torch.device('cpu')
        return self._device_num


class RegionLoss(BaseLossWithCudaState):
    """ Computes region loss from darknet network output and target annotation.

    Args:
        num_classes (int): number of categories
        anchors (list): 2D list representing anchor boxes (see :class:`lightnet.network.Darknet`)
            These width and height values should be in network output coordinates.
        coord_scale (float): weight of bounding box coordinates
        noobject_scale (float): weight of regions without target boxes
        object_scale (float): weight of regions with target boxes
        class_scale (float): weight of categorical predictions
        thresh (float): minimum iou for a predicted box to be assigned to a target

    CommandLine:
        python ~/code/netharn/netharn/models/yolo2/light_region_loss.py RegionLoss

    Example:
        >>> from netharn.models.yolo2.light_yolo import Yolo
        >>> torch.random.manual_seed(0)
        >>> network = Yolo(num_classes=2, conf_thresh=4e-2)
        >>> self = RegionLoss(num_classes=network.num_classes, anchors=network.anchors)
        >>> Win, Hin = 96, 96
        >>> Wout, Hout = 1, 1
        >>> # true boxes for each item in the batch
        >>> # each box encodes class, center, width, and height
        >>> # coordinates are normalized in the range 0 to 1
        >>> # items in each batch are padded with dummy boxes with class_id=-1
        >>> target = torch.FloatTensor([
        >>>     # boxes for batch item 1
        >>>     [[0, 0.50, 0.50, 1.00, 1.00],
        >>>      [1, 0.32, 0.42, 0.22, 0.12]],
        >>>     # boxes for batch item 2 (it has no objects, note the pad!)
        >>>     [[-1, 0, 0, 0, 0],
        >>>      [-1, 0, 0, 0, 0]],
        >>> ])
        >>> im_data = torch.randn(len(target), 3, Hin, Win)
        >>> output = network.forward(im_data)
        >>> loss = float(self.forward(output, target))
        >>> print(f'loss = {loss:.2f}')
        >>> print(f'output.sum() = {output.sum():.2f}')

        loss = 20.18
        output.sum() = 2.15

    Example:
        >>> from netharn.models.yolo2.light_yolo import Yolo
        >>> torch.random.manual_seed(0)
        >>> network = Yolo(num_classes=2, conf_thresh=4e-2)
        >>> self = RegionLoss(num_classes=network.num_classes, anchors=network.anchors)
        >>> Win, Hin = 96, 96
        >>> Wout, Hout = 1, 1
        >>> target = torch.FloatTensor([])
        >>> im_data = torch.randn(2, 3, Hin, Win)
        >>> output = network.forward(im_data)
        >>> loss = float(self.forward(output, target))
        >>> print(f'output.sum() = {output.sum():.2f}')
        >>> print(f'loss = {loss:.2f}')

        output.sum() = 2.15
        loss = 16.47
    """

    def __init__(self, num_classes, anchors, coord_scale=1.0,
                 noobject_scale=1.0, object_scale=5.0, class_scale=1.0,
                 thresh=0.6):
        super().__init__()

        self.num_classes = num_classes

        self.anchors = torch.Tensor(anchors)
        self.num_anchors = len(anchors)

        # self.anchor_step = len(self.anchors) // self.num_anchors
        self.reduction = 32             # input_dim/output_dim

        self.coord_scale = coord_scale
        self.noobject_scale = noobject_scale
        self.object_scale = object_scale
        self.class_scale = class_scale
        self.thresh = thresh

        self.loss_coord = None
        self.loss_conf = None
        self.loss_cls = None
        self.loss_tot = None

        self.coord_mse = nn.MSELoss(size_average=False)
        self.conf_mse = nn.MSELoss(size_average=False)
        self.cls_critrion = nn.CrossEntropyLoss(size_average=False)

        # Precompute relative anchors in tlbr format for iou computation
        rel_anchors_cxywh = torch.cat([torch.zeros_like(self.anchors), self.anchors], 1)
        self.rel_anchors_boxes = util.Boxes(rel_anchors_cxywh, 'cxywh')

        self._prev_pred_init = None
        self._prev_pred_dim = None

        self.iou_mode = None

    @profiler.profile
    def forward(self, output, target, seen=0, gt_weights=None):
        """ Compute Region loss.

        Args:
            output (torch.autograd.Variable): Output from the network
                should have shape [B, A, 5 + C, H, W]

            target (torch.Tensor): the shape should be [B, T, 5], where B is
                the batch size, T is the maximum number of boxes in an item,
                and the final dimension should correspond to [class_idx,
                center_x, center_y, width, height]. Items with fewer than T
                boxes should be padded with dummy boxes with class_idx=-1.

            seen (int): number of training batches the networks has "seen"

        Example:
            >>> nC = 2
            >>> self = RegionLoss(num_classes=nC, anchors=np.array([[1, 1]]))
            >>> nA = len(self.anchors)
            >>> # one batch, with one anchor, with 2 classes and 3x3 grid cells
            >>> output = torch.rand(1, nA, 5 + nC, 3, 3)
            >>> # one batch, with one true box
            >>> target = torch.rand(1, 1, 5)
            >>> target[..., 0] = 0
            >>> seen = 0
            >>> gt_weights = None
            >>> self.forward(output, target, seen)
        """
        # Parameters
        nB, nA, nC5, nH, nW = output.data.shape
        nC = self.num_classes
        assert nA == self.num_anchors
        assert nC5 == self.num_classes + 5

        device = self.get_device()
        self.rel_anchors_boxes.data = self.rel_anchors_boxes.data.to(device)
        self.anchors = self.anchors.to(device)

        # if isinstance(target, Variable):
        #     target = target.data

        # Get x,y,w,h,conf,*cls_probs from the third dimension
        # output_ = output.view(nB, nA, 5 + nC, nH, nW)

        coord = torch.zeros_like(output[:, :, 0:4, :, :])
        coord[:, :, 0:2, :, :] = output[:, :, 0:2, :, :].sigmoid()  # tx,ty
        coord[:, :, 2:4, :, :] = output[:, :, 2:4, :, :]            # tw,th

        conf = output[:, :, 4:5, :, :].sigmoid()
        if nC > 1:
            # Swaps the dimensions from [B, A, C, H, W] to be [B, A, H, W, C]
            cls_probs = output[:, :, 5:, :, :].contiguous().view(nB * nA, nC, nH * nW).transpose(1, 2).contiguous().view(nB, nA, nH, nW, nC)

        with torch.no_grad():
            # Create prediction boxes
            pred_cxywh = torch.empty(nB * nA * nH * nW, 4, dtype=torch.float32, device=device)

            # Grid cell center offsets
            lin_x = torch.linspace(0, nW - 1, nW).repeat(nH, 1).to(device)
            lin_y = torch.linspace(0, nH - 1, nH).repeat(nW, 1).t().contiguous().to(device)
            anchor_w = self.anchors[:, 0].contiguous().view(nA, 1).view(1, nA, 1, 1, 1)
            anchor_h = self.anchors[:, 1].contiguous().view(nA, 1).view(1, nA, 1, 1, 1)

            # Convert raw network output to bounding boxes in network output coordinates
            pred_cxywh[:, 0] = (coord[:, :, 0:1, :, :].data + lin_x).view(-1)
            pred_cxywh[:, 1] = (coord[:, :, 1:2, :, :].data + lin_y).view(-1)
            pred_cxywh[:, 2] = (coord[:, :, 2:3, :, :].data.exp() * anchor_w).view(-1)
            pred_cxywh[:, 3] = (coord[:, :, 3:4, :, :].data.exp() * anchor_h).view(-1)

            # Get target values
            _tup = self.build_targets(
                pred_cxywh, target, nH, nW, seen=seen, gt_weights=gt_weights)
            coord_mask, conf_mask, cls_mask, tcoord, tconf, tcls = _tup

            if nC > 1:
                masked_tcls = tcls[cls_mask].view(-1).long()

        tcoord = Variable(tcoord, requires_grad=False)
        tconf = Variable(tconf, requires_grad=False)
        coord_mask = Variable(coord_mask, requires_grad=False)
        conf_mask = Variable(conf_mask, requires_grad=False)
        if nC > 1:
            tcls = Variable(tcls, requires_grad=False)
            # Swaps the dimensions to be [B, A, H, W, C]
            # (Allowed because 3rd dimension is guarneteed to be 1 here)
            cls_probs_mask = cls_mask.reshape(nB, nA, nH, nW, 1).repeat(1, 1, 1, 1, nC)
            cls_probs_mask = Variable(cls_probs_mask, requires_grad=False)
            masked_cls_probs = cls_probs[cls_probs_mask].view(-1, nC)

        # Compute losses

        # Bounding Box Loss
        # To be compatible with the original YOLO code we add a seemingly
        # random multiply by .5 in our MSE computation so the torch autodiff
        # algorithm produces the same result as darknet.
        loss_coord = 0.5 * self.coord_scale * self.coord_mse(
            coord_mask * coord, coord_mask * tcoord) / nB

        # Objectness Loss
        # object_scale and noobject_scale are incorporated in conf_mask.
        loss_conf = 0.5 * self.conf_mse(conf_mask * conf,
                                        conf_mask * tconf) / nB

        # Class Loss
        if nC > 1 and masked_cls_probs.numel():
            loss_cls = self.class_scale * self.cls_critrion(masked_cls_probs,
                                                            masked_tcls) / nB
            self.loss_cls = float(loss_cls.data.cpu().numpy())
        else:
            self.loss_cls = loss_cls = 0

        loss_tot = loss_coord + loss_conf + loss_cls

        # Record loss components as module members
        self.loss_tot = float(loss_tot.data.cpu().numpy())
        self.loss_coord = float(loss_coord.data.cpu().numpy())
        self.loss_conf = float(loss_conf.data.cpu().numpy())

        return loss_tot

    @profiler.profile
    def build_targets(self, pred_cxywh, target, nH, nW, seen=0, gt_weights=None):
        """
        Compare prediction boxes and targets, convert targets to network output tensors

        Args:
            pred_cxywh (Tensor):   shape [B * A * W * H, 4] in normalized cxywh format
            target (Tensor): shape [B, max(gtannots), 4]

        CommandLine:
            python ~/code/netharn/netharn/models/yolo2/light_region_loss.py RegionLoss.build_targets:1

        Example:
            >>> from netharn.models.yolo2.light_yolo import Yolo
            >>> from netharn.models.yolo2.light_region_loss import RegionLoss
            >>> torch.random.manual_seed(0)
            >>> network = Yolo(num_classes=2, conf_thresh=4e-2)
            >>> self = RegionLoss(num_classes=network.num_classes, anchors=network.anchors)
            >>> Win, Hin = 96, 96
            >>> nW, nH = 3, 3
            >>> target = torch.FloatTensor([])
            >>> gt_weights = torch.FloatTensor([[-1, -1, -1], [1, 1, 0]])
            >>> #pred_cxywh = torch.rand(90, 4)
            >>> nB = len(gt_weights)
            >>> pred_cxywh = torch.rand(nB, len(self.anchors), nH, nW, 4).view(-1, 4)
            >>> seen = 0
            >>> self.build_targets(pred_cxywh, target, nH, nW, seen, gt_weights)

        Example:
            >>> from netharn.models.yolo2.light_region_loss import RegionLoss
            >>> torch.random.manual_seed(0)
            >>> anchors = np.array([[.75, .75], [1.0, .3], [.3, 1.0]])
            >>> self = RegionLoss(num_classes=2, anchors=anchors)
            >>> nW, nH = 2, 2
            >>> # true boxes for each item in the batch
            >>> # each box encodes class, center, width, and height
            >>> # coordinates are normalized in the range 0 to 1
            >>> # items in each batch are padded with dummy boxes with class_id=-1
            >>> target = torch.FloatTensor([
            >>>     # boxes for batch item 0 (it has no objects, note the pad!)
            >>>     [[-1, 0, 0, 0, 0],
            >>>      [-1, 0, 0, 0, 0],
            >>>      [-1, 0, 0, 0, 0]],
            >>>     # boxes for batch item 1
            >>>     [[0, 0.50, 0.50, 1.00, 1.00],
            >>>      [1, 0.34, 0.32, 0.12, 0.32],
            >>>      [1, 0.32, 0.42, 0.22, 0.12]],
            >>> ])
            >>> gt_weights = torch.FloatTensor([[-1, -1, -1], [1, 1, 0]])
            >>> nB = len(gt_weights)
            >>> pred_cxywh = torch.rand(nB, len(anchors), nH, nW, 4).view(-1, 4)
            >>> seen = 0
            >>> coord_mask, conf_mask, cls_mask, tcoord, tconf, tcls = self.build_targets(pred_cxywh, target, nH, nW, seen, gt_weights)
        """
        gtempty = (target.numel() == 0)

        # Parameters
        nB = target.shape[0] if not gtempty else 0
        # nT = target.shape[1] if not gtempty else 0
        nA = self.num_anchors

        if nB == 0:
            # torch does not preserve shapes when any dimension goes to 0
            # fix nB if there is no groundtruth
            nB = int(len(pred_cxywh) / (nA * nH * nW))
        else:
            assert nB == int(len(pred_cxywh) / (nA * nH * nW)), 'bad assumption'

        seen = seen + nB

        # Tensors
        device = self.get_device()

        # Put the groundtruth in a format comparable to output
        tcoord = torch.zeros(nB, nA, 4, nH, nW, device=device)
        tconf = torch.zeros(nB, nA, 1, nH, nW, device=device)
        tcls = torch.zeros(nB, nA, 1, nH, nW, device=device)

        # Create weights to determine which outputs are punished
        # By default we punish all outputs for not having correct iou
        # objectness prediction. The other masks default to zero meaning that
        # by default we will not punish a prediction for having a different
        # coordinate or class label (later the groundtruths will override these
        # defaults for select grid cells and anchors)
        coord_mask = torch.zeros(nB, nA, 1, nH, nW, device=device)
        conf_mask = torch.ones(nB, nA, 1, nH, nW, device=device)
        cls_mask = torch.zeros(nB, nA, 1, nH, nW, device=device).byte()

        # Default conf_mask to the noobject_scale
        conf_mask.fill_(self.noobject_scale)

        # encourage the network to predict boxes centered on the grid cells by
        # setting the default target xs and ys to be (.5, .5) (i.e. the
        # relative center of a grid cell) fill the mask with ones so all
        # outputs are punished for not predicting center anchor locations ---
        # unless tcoord is overriden by a real groundtruth target later on.
        if seen < 12800:
            # PJreddies version
            # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L254

            # By default encourage the network to predict no shift
            tcoord[:, :, 0:2, :, :].fill_(0.5)
            # By default encourage the network to predict no scale (in logspace)
            tcoord[:, :, 0:2, :, :].fill_(0.0)
            # In the warmup phase we care about changing the coords to be
            # exactly the anchors if they don't predict anything, but the
            # weight is only 0.01, set it to 0.01 / self.coord_scale.
            # Note we will apply the required sqrt later
            coord_mask.fill_((0.01 / self.coord_scale))

        if gtempty:
            coord_mask = coord_mask.sqrt()
            conf_mask = conf_mask.sqrt()
            coord_mask = coord_mask.expand_as(tcoord)
            return coord_mask, conf_mask, cls_mask, tcoord, tconf, tcls

        # Put this back into a non-flat view
        pred_cxywh = pred_cxywh.view(nB, nA, nH, nW, 4)
        pred_boxes = util.Boxes(pred_cxywh, 'cxywh')

        gt_class = target[..., 0].data
        gt_boxes_norm = util.Boxes(target[..., 1:5], 'cxywh')
        gt_boxes = gt_boxes_norm.scale([nW, nH])
        # Construct "relative" versions of the true boxes, centered at 0
        # This will allow them to be compared to the anchor boxes.
        rel_gt_boxes = gt_boxes.copy()
        rel_gt_boxes.data[..., 0:2] = 0

        # true boxes with a class of -1 are fillers, ignore them
        gt_isvalid = (gt_class >= 0)

        # Compute the grid cell for each groundtruth box
        true_xs, true_ys = gt_boxes.components[0:2]
        true_is = true_xs.long().clamp_(0, nW - 1)
        true_js = true_ys.long().clamp_(0, nH - 1)

        if gt_weights is None:
            # If unspecified give each groundtruth a default weight of 1
            gt_weights = torch.ones_like(target[..., 0], device=device)

        # Undocumented darknet detail: multiply coord weight by two minus the
        # area of the true box in normalized coordinates.  the square root is
        # because the weight.
        gt_coord_weights = (gt_weights * (2.0 - gt_boxes_norm.area[..., 0]))

        # Loop over ground_truths and construct tensors
        for bx in range(nB):
            # Get the actual groundtruth boxes for this batch item
            flags = gt_isvalid[bx]
            if not np.any(flags):
                continue

            # Batch ground truth
            batch_rel_gt_boxes = rel_gt_boxes[bx, flags]
            cur_gt_boxes = gt_boxes[bx, flags]
            cur_true_is = true_is[bx, flags]
            cur_true_js = true_js[bx, flags]
            cur_true_weights = gt_weights[bx, flags]
            cur_true_coord_weights = gt_coord_weights[bx, flags]

            # Batch predictions
            cur_pred_boxes = pred_boxes[bx]

            # Assign groundtruth boxes to anchor boxes
            anchor_ious = self.rel_anchors_boxes.ious(batch_rel_gt_boxes, bias=0)
            _, best_anchor_idxs = anchor_ious.max(dim=0)  # best_ns in YOLO

            # Assign groundtruth boxes to predicted boxes
            ious = cur_pred_boxes.ious(cur_gt_boxes, bias=0)
            cur_ious, _ = ious.max(dim=-1)

            # Set loss to zero for any predicted boxes that had a high iou with
            # a groundtruth target (we wont punish them for not being
            # background), One of these will be selected as the best and be
            # punished for not predicting the groundtruth value.
            conf_mask[bx].view(-1)[cur_ious.view(-1) > self.thresh] = 0

            for t in range(cur_gt_boxes.shape[0]):
                gt_box_ = cur_gt_boxes[t]
                weight = cur_true_weights[t]
                # coord weights incorporate weight and true box area
                coord_weight = cur_true_coord_weights[t]

                # The assigned (best) anchor index
                ax = best_anchor_idxs[t].item()
                anchor_w, anchor_h = self.anchors[ax]

                # Compute this ground truth's grid cell
                gx, gy, gw, gh = gt_box_.data
                gi = cur_true_is[t].item()
                gj = cur_true_js[t].item()

                # The prediction will be punished if it does not match this true box
                # pred_box_ = cur_pred_boxes[best_n, gj, gi]

                # Get the precomputed iou of the truth with this box
                # corresponding to the assigned anchor and grid cell
                iou = ious[ax, gj, gi, t].item()

                # Mark that we will care about the predicted box with some weight
                coord_mask[bx, ax, 0, gj, gi] = coord_weight

                # PJReddie delta_region_class:
                # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L112
                # https://github.com/pjreddie/darknet/blob/master/src/region_layer.c#L314
                cls_mask[bx, ax, 0, gj, gi] = int(weight > .5)

                conf_mask[bx, ax, 0, gj, gi] = self.object_scale * weight

                # The true box is converted into coordinates comparable to the
                # network outputs by:
                # (1) we center the true box on its assigned grid cell
                # (2) we divide its width and height by its assigned anchor
                # (3) we take the log of width and height because the raw
                #     network wh outputs are in logspace.
                tcoord[bx, ax, 0, gj, gi] = gx - gi
                tcoord[bx, ax, 1, gj, gi] = gy - gj
                tcoord[bx, ax, 2, gj, gi] = math.log(gw / anchor_w)
                tcoord[bx, ax, 3, gj, gi] = math.log(gh / anchor_h)
                tconf[bx, ax, 0, gj, gi] = iou  # if rescore else 1
                tcls[bx, ax, 0, gj, gi] = target[bx, t, 0]

        # because coord and conf masks are witin this MSE we need to sqrt them
        coord_mask = coord_mask.sqrt()
        conf_mask = conf_mask.sqrt()
        coord_mask = coord_mask.expand_as(tcoord)

        # masked_tcls = tcls[cls_mask].view(-1).long()
        # cls_probs_mask = cls_mask.reshape(nB, nA, nH, nW, 1).repeat(1, 1, 1, 1, nC)
        # cls_probs_mask = Variable(cls_probs_mask, requires_grad=False)
        # masked_cls_probs = cls_probs[cls_probs_mask].view(-1, nC)

        return coord_mask, conf_mask, cls_mask, tcoord, tconf, tcls


if __name__ == '__main__':
    """
    CommandLine:
        python -m netharn.models.yolo2.light_region_loss all
    """
    import xdoctest
    xdoctest.doctest_module(__file__)

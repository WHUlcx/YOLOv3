# coding: utf-8

from __future__ import division, print_function

import numpy as np
import cv2
import sys
from utils.data_aug import *
import random

PY_VERSION = sys.version_info[0]
iter_cnt = 0


def parse_line(line):
    '''
    Given a line from the training/test txt file, return parsed info.
    line format: line_index, img_path, img_width, img_height, [box_info_1 (5 number)], ...
    return:
        line_idx: int64
        pic_path: string.
        boxes: shape [N, 4], N is the ground truth count, elements in the second
            dimension are [x_min, y_min, x_max, y_max]
        labels: shape [N]. class index.
        img_width: int.
        img_height: int
    '''
    #保证他为str类型才能进行读出
    if 'str' not in str(type(line)):
        line = line.decode()
    s = line.strip().split(' ')
    assert len(s) > 8, 'Annotation error! Please check your annotation file. Make sure there is at least one target object in each image.'
    line_idx = int(s[0])
    pic_path = s[1]
    img_width = int(s[2])
    img_height = int(s[3])
    s = s[4:]
    assert len(s) % 5 == 0, 'Annotation error! Please check your annotation file. Maybe partially missing some coordinates?'
    #box的数量
    box_cnt = len(s) // 5
    boxes = []
    labels = []
    for i in range(box_cnt):
        label, x_min, y_min, x_max, y_max = int(s[i * 5]), float(s[i * 5 + 1]), float(s[i * 5 + 2]), float(
            s[i * 5 + 3]), float(s[i * 5 + 4])
        boxes.append([x_min, y_min, x_max, y_max])
        labels.append(label)
    boxes = np.asarray(boxes, np.float32)
    labels = np.asarray(labels, np.int64)
    return line_idx, pic_path, boxes, labels, img_width, img_height


def process_box(boxes, labels, img_size, class_num, anchors):
    '''
    #我这个地方就是论文里面负责筛选anchor box 目标性评分的部分，出来的就是目标性评分最高的anchor box对应的bounding box
    Generate the y_true label, i.e. the ground truth feature_maps in 3 different scales.
    params:
        boxes: [N, 5] shape, float32 dtype. `x_min, y_min, x_max, y_mix, mixup_weight`.
        labels: [N] shape, int64 dtype.
        class_num: int64 num.
        anchors: [9, 2] shape, float32 dtype.
    '''
    anchors_mask = [[6, 7, 8], [3, 4, 5], [0, 1, 2]]

    # convert boxes form:
    # shape: [N, 2]
    # (x_center, y_center)
    box_centers = (boxes[:, 0:2] + boxes[:, 2:4]) / 2
    # (width, height)
    box_sizes = boxes[:, 2:4] - boxes[:, 0:2]

    # [13, 13, 3, 5+num_class+1] `5` means coords and labels. `1` means mix up weight. 
    y_true_13 = np.zeros((img_size[1] // 32, img_size[0] // 32, 3, 6 + class_num), np.float32)
    y_true_26 = np.zeros((img_size[1] // 16, img_size[0] // 16, 3, 6 + class_num), np.float32)
    y_true_52 = np.zeros((img_size[1] // 8, img_size[0] // 8, 3, 6 + class_num), np.float32)

    # mix up weight default to 1.
    y_true_13[..., -1] = 1.
    y_true_26[..., -1] = 1.
    y_true_52[..., -1] = 1.

    y_true = [y_true_13, y_true_26, y_true_52]

    # [N, 1, 2]
    #本来是N,2的维度，在1的位置加了一个dimension
    box_sizes = np.expand_dims(box_sizes, 1)
    # 自动broadcast tricks
    # [N, 1, 2] & [9, 2] ==> [N, 9, 2]
    #用来算IOU，假设中心点都在原点
    mins = np.maximum(- box_sizes / 2, - anchors / 2)
    maxs = np.minimum(box_sizes / 2, anchors / 2)
    # [N, 9, 2]
    whs = maxs - mins

    # [N, 9]
    iou = (whs[:, :, 0] * whs[:, :, 1]) / (
                box_sizes[:, :, 0] * box_sizes[:, :, 1] + anchors[:, 0] * anchors[:, 1] - whs[:, :, 0] * whs[:, :,
                                                                                                         1] + 1e-10)
    # [N]，我们认为，基本上一个格子里只会有一个相应大小的ground truth box，所以只找最大的，返回的是索引
    best_match_idx = np.argmax(iou, axis=1)
    #缩放比例，字典类型
    ratio_dict = {1.: 8., 2.: 16., 3.: 32.}
    for i, idx in enumerate(best_match_idx):
        # idx: 0,1,2 ==> 2; 3,4,5 ==> 1; 6,7,8 ==> 0
        feature_map_group = 2 - idx // 3
        # scale ratio: 0,1,2 ==> 8; 3,4,5 ==> 16; 6,7,8 ==> 32
        ratio = ratio_dict[np.ceil((idx + 1) / 3.)]#向上取整决定缩放比例
        x = int(np.floor(box_centers[i, 0] / ratio))#向下取整决定框的位置
        y = int(np.floor(box_centers[i, 1] / ratio))
        k = anchors_mask[feature_map_group].index(idx)#返回这是几号anchor box，在哪一列，对应的就是在单独的一个cell里选择哪一个框
        #对应的第i个格子相应的物体类型
        c = labels[i]
        # print(feature_map_group, '|', y,x,k,c)
        #feature map为0，就是13，后以此类推

        y_true[feature_map_group][y, x, k, :2] = box_centers[i]
        y_true[feature_map_group][y, x, k, 2:4] = box_sizes[i]
        y_true[feature_map_group][y, x, k, 4] = 1.#根据IOU选拔出来的，自然confidence为1
        y_true[feature_map_group][y, x, k, 5 + c] = 1.#是这个物体类型的ground truth box
        y_true[feature_map_group][y, x, k, -1] = boxes[i, -1] #mix_up weight

    return y_true_13, y_true_26, y_true_52


def parse_data(line, class_num, img_size, anchors, mode, letterbox_resize):
    '''
    param:
        line: a line from the training/test txt file
        class_num: totol class nums.
        img_size: the size of image to be resized to. [width, height] format.
        anchors: anchors.
        mode: 'train' or 'val'. When set to 'train', data_augmentation will be applied.
        letterbox_resize: whether to use the letterbox resize, i.e., keep the original aspect ratio in the resized image.
    '''
    if not isinstance(line, list):
        img_idx, pic_path, boxes, labels, _, _ = parse_line(line)
        img = cv2.imread(pic_path)
        # expand the 2nd dimension, mix up weight default to 1.
        boxes = np.concatenate((boxes, np.full(shape=(boxes.shape[0], 1), fill_value=1., dtype=np.float32)), axis=-1)
    else:
        # the mix up case
        _, pic_path1, boxes1, labels1, _, _ = parse_line(line[0])
        img1 = cv2.imread(pic_path1)
        img_idx, pic_path2, boxes2, labels2, _, _ = parse_line(line[1])
        img2 = cv2.imread(pic_path2)

        img, boxes = mix_up(img1, img2, boxes1, boxes2)
        labels = np.concatenate((labels1, labels2))

    if mode == 'train':
        # random color jittering
        # NOTE: applying color distort may lead to bad performance sometimes
        #随机颜色改变
        img = random_color_distort(img)

        # random expansion with prob 0.5
        #随机填充
        if np.random.uniform(0, 1) > 0.5:
            img, boxes = random_expand(img, boxes, 4)

        # random cropping
        #随机剪裁
        h, w, _ = img.shape
        boxes, crop = random_crop_with_constraints(boxes, (w, h))
        x0, y0, w, h = crop
        img = img[y0: y0+h, x0: x0+w]

        # resize with random interpolation
        #随机插值
        h, w, _ = img.shape
        interp = np.random.randint(0, 5)
        img, boxes = resize_with_bbox(img, boxes, img_size[0], img_size[1], interp=interp, letterbox=letterbox_resize)

        # random horizontal flip
        #随机水平翻转
        h, w, _ = img.shape
        img, boxes = random_flip(img, boxes, px=0.5)
    else:
        img, boxes = resize_with_bbox(img, boxes, img_size[0], img_size[1], interp=1, letterbox=letterbox_resize)

    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)

    # the input of yolo_v3 should be in range 0~1
    img = img / 255.

    y_true_13, y_true_26, y_true_52 = process_box(boxes, labels, img_size, class_num, anchors)

    return img_idx, img, y_true_13, y_true_26, y_true_52


def get_batch_data(batch_line, class_num, img_size, anchors, mode, multi_scale=False, mix_up=False, letterbox_resize=True, interval=10):
    '''
    所说的多尺度训练，隔一段epoches换一个resolution
    generate a batch of imgs and labels
    param:
        batch_line: a batch of lines from train/val.txt files
        class_num: num of total classes.
        img_size: the image size to be resized to. format: [width, height].
        anchors: anchors. shape: [9, 2].
        mode: 'train' or 'val'. if set to 'train', data augmentation will be applied.
        multi_scale: whether to use multi_scale training, img_size varies from [320, 320] to [640, 640] by default. Note that it will take effect only when mode is set to 'train'.
        letterbox_resize: whether to use the letterbox resize, i.e., keep the original aspect ratio in the resized image.
        interval: change the scale of image every interval batches. Note that it's indeterministic because of the multi threading.
    '''
    global iter_cnt
    # multi_scale training
    #使用多尺度以及训练模式
    if multi_scale and mode == 'train':
        random.seed(iter_cnt // interval)
        #从320-608，符合论文里的32倍降采样率
        random_img_size = [[x * 32, x * 32] for x in range(10, 20)]
        #随便挑一个size
        img_size = random.sample(random_img_size, 1)[0]
    iter_cnt += 1

    img_idx_batch, img_batch, y_true_13_batch, y_true_26_batch, y_true_52_batch = [], [], [], [], []

    # mix up strategy
    if mix_up and mode == 'train':
        mix_lines = []
        #数组转列表
        batch_line = batch_line.tolist()
        for idx, line in enumerate(batch_line):
            if np.random.uniform(0, 1) < 0.5:
                #随机采样
                mix_lines.append([line, random.sample(batch_line[:idx] + batch_line[idx+1:], 1)[0]])
            else:
                mix_lines.append(line)
        #随机得到的batch_lines
        batch_line = mix_lines

    for line in batch_line:
        img_idx, img, y_true_13, y_true_26, y_true_52 = parse_data(line, class_num, img_size, anchors, mode, letterbox_resize)

        img_idx_batch.append(img_idx)
        img_batch.append(img)
        y_true_13_batch.append(y_true_13)
        y_true_26_batch.append(y_true_26)
        y_true_52_batch.append(y_true_52)

    img_idx_batch, img_batch, y_true_13_batch, y_true_26_batch, y_true_52_batch = np.asarray(img_idx_batch, np.int64), np.asarray(img_batch), np.asarray(y_true_13_batch), np.asarray(y_true_26_batch), np.asarray(y_true_52_batch)

    return img_idx_batch, img_batch, y_true_13_batch, y_true_26_batch, y_true_52_batch

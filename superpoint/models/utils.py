import tensorflow as tf
from math import pi

from .backbones.vgg import vgg_block


def detector_head(inputs, **config):
    params_conv = {'padding': 'SAME', 'data_format': config['data_format'],
                   'activation': tf.nn.relu, 'batch_normalization': True,
                   'training': config['training']}
    cfirst = config['data_format'] == 'channels_first'
    cindex = 1 if cfirst else -1  # index of the channel

    with tf.variable_scope('detector'):
        x = vgg_block(inputs, 256, 3, 'conv1', **params_conv)
        x = vgg_block(inputs, 1+pow(config['grid_size'], 2), 1, 'conv2', **params_conv)

        prob = tf.nn.softmax(x, axis=cindex)
        # Strip the extra “no interest point” dustbin
        prob = prob[:, :-1, :, :] if cfirst else prob[:, :, :, :-1]
        prob = tf.depth_to_space(
                prob, config['grid_size'], data_format='NCHW' if cfirst else 'NHWC')
        prob = tf.squeeze(prob, axis=cindex)

        pred = tf.to_int32(tf.greater_equal(prob, config['detection_threshold']))

    return {'logits': x, 'prob': prob, 'pred': pred}


def homography_adaptation(image, net, config):
    """Perfoms homography adaptation.

    Inference using multiple random wrapped patches of the same input image for robust
    predictions.

    Arguments:
        image: A `Tensor` with shape `[H, W, 1]`.
        net: A function that takes an image as input, performs inference, and outputs the
            prediction dictionary.
        config: A configuration dictionary, which contains the sub-dictionary
            `'homography_adapatation'`, with various parameters such as the number of
            sampled homographies `'num'`.

    Returns:
        A dictionary which contains the final inference results, i.e. the detection
        probabilities and the thresholded predictions.
    """

    prob = net(image)['prob']
    counts = tf.ones_like(prob, dtype=tf.int32)

    def step(i, prob, counts):
        # TODO:
        # sample random homography
        # extract patch from image given the homography
        # obtain prediction
        # invert homography
        # get patch from predictions
        # fuse with existing prob and counts
        return i + 1, prob, counts

    _, prob, counts = tf.while_loop(
            lambda i, p, c: tf.less(i, config['homography_adaptation']['num'] - 1),
            step,
            [0, prob, counts])

    prob /= counts
    pred = tf.to_int32(tf.greater_equal(prob, config['detection_threshold']))
    return {'prob': prob, 'pred': pred}


def sample_homography(
        shape, perspective=True, scaling=True, rotation=True, translation=True,
        n_scales=10, n_angles=10):
    """Sample a random valid homography.

    Computes the homography transformation between a random patch in the orignal image
    and a wrapped projection with the same image size.
    As in `tf.contrib.image.transform`, it maps the output point (wrapped patch) to a
    transformed input point (orginal patch).
    The original patch, which is intialized with a simple half-size centered crop, is
    iteratively projected, scaled, rotated and translated.

    Arguments:
        shape: A rank-2 `Tensor` specifying the height and width of the original image.
        prespective: A boolean that enables the perspective and affine transformations.
        scaling: A boolean that enables the random scaling of the patch.
        rotation: A boolean that enables the random rotation of the patch.
        translation: A boolean that enables the random translation of the patch.
        n_scales: the number of tentative scales that are sampled when scaling.
        n_angles: the number of tentatives angles that are sampled when rotating.

    Returns:
        A `Tensor` of shape `[1, 8]` corresponding to the flattened homography transform.
    """

    # Corners of the output image
    pts1 = tf.stack([[0., 0.], [0., 1.], [1., 1.], [1., 0.]], axis=0)
    # Corners of the input patch
    pts2 = 0.25 + tf.constant([[0, 0], [0, 0.5], [0.5, 0.5], [0.5, 0]], tf.float32)

    # Random perspective and affine perturbations
    if perspective:
        pts2 += tf.truncated_normal([4, 2], 0., 0.25/2)

    # Random scaling
    # sample several scales, check collision with borders, randomly pick a valid one
    if scaling:
        scales = tf.concat([[1.], tf.truncated_normal([n_scales], 1, 0.75/2)], 0)
        center = tf.reduce_mean(pts2, axis=0, keepdims=True)
        scaled = tf.expand_dims(pts2 - center, axis=0) * tf.expand_dims(
                tf.expand_dims(scales, 1), 1) + center
        valid = tf.logical_and(tf.greater_equal(scaled, 0.), tf.less(scaled, 1.))
        valid = tf.where(tf.reduce_all(valid, axis=[1, 2]))
        idx = tf.random_shuffle(valid)[0]
        pts2 = scaled[idx[0]]

    # Random rotation
    # sample several rotations, check collision with borders, randomly pick a valid one
    if rotation:
        angles = tf.lin_space(0., 2*tf.constant(pi), n_angles)
        center = tf.reduce_mean(pts2, axis=0, keepdims=True)
        rot_mat = tf.reshape(tf.stack([tf.cos(angles), -tf.sin(angles), tf.sin(angles),
                                       tf.cos(angles)], axis=1), [-1, 2, 2])
        rotated = tf.matmul(
                tf.tile(tf.expand_dims(pts2 - center, axis=0), [n_angles, 1, 1]),
                rot_mat) + center
        valid = tf.logical_and(tf.greater_equal(rotated, 0.), tf.less(rotated, 1.))
        valid = tf.where(tf.reduce_all(valid, axis=[1, 2]))
        idx = tf.random_shuffle(valid)[0]
        pts2 = rotated[idx[0]]

    # Random translation
    if translation:
        t_min, t_max = tf.reduce_min(pts2, axis=0), tf.reduce_min(1 - pts2, axis=0)
        pts2 += tf.expand_dims(tf.stack([tf.random_uniform((), -t_min[0], t_max[0]),
                                         tf.random_uniform((), -t_min[1], t_max[1])]),
                               axis=0)

    # Rescale to actual size
    shape = tf.to_float(shape[::-1])  # different convention [y, x]
    pts1 *= tf.expand_dims(shape, axis=0)
    pts2 *= tf.expand_dims(shape, axis=0)

    def ax(p, q): return [p[0], p[1], 1, 0, 0, 0, -p[0] * q[0], -p[1] * q[0]]

    def ay(p, q): return [0, 0, 0, p[0], p[1], 1, -p[0] * q[1], -p[1] * q[1]]

    a_mat = tf.stack([f(pts1[i], pts2[i]) for i in range(4) for f in (ax, ay)], axis=0)
    p_mat = tf.transpose(tf.stack(
        [[pts2[i][j] for i in range(4) for j in range(2)]], axis=0))
    homography = tf.transpose(tf.matrix_solve_ls(a_mat, p_mat, fast=True))
    return homography


def invert_homography(H):
    """
    Computes the inverse transformation for a flattened homography transformation.
    """
    return mat2flat(tf.matrix_inverse(flat2mat(H)))


def flat2mat(H):
    """
    Converts a flattened homography transformation with shape `[1, 8]` to its
    corresponding homography matrix with shape `[1, 9, 9]`.
    """
    return tf.reshape(tf.concat([H, tf.ones([tf.shape(H)[0], 1])], axis=1), [-1, 3, 3])


def mat2flat(H):
    """
    Converts an homography matrix with shape `[1, 9, 9]` to its corresponding flattened
    homography transformation with shape `[1, 8]`.
    """
    H = tf.reshape(H, [-1, 9])
    return (H / H[:, 8:9])[:, :8]

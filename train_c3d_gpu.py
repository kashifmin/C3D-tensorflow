# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Trains and Evaluates the 3d convolutional neural network using a feed 
    dictionary.
"""
import os
import time


from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
import math
import numpy as np
import input_data
import c3d_model

# Basic model parameters as external flags.
FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string('train_dir', './result',
                           """Directory where to write event logs """
                           """and checkpoint.""")
tf.app.flags.DEFINE_integer('gpu_num', 1, 
                            """How many GPUs to use""")
tf.app.flags.DEFINE_integer('max_steps', 100000, 
                            """Number of batches to run.""")
tf.app.flags.DEFINE_integer('batch_size', 10,
                            """Batch size.""")


def placeholder_inputs(batch_size):
  """Generate placeholder variables to represent the input tensors.

  These placeholders are used as inputs by the rest of the model building
  code and will be fed from the downloaded data in the .run() loop, below.

  Args:
    batch_size: The batch size will be baked into both placeholders.

  Returns:
    images_placeholder: Images placeholder.
    labels_placeholder: Labels placeholder.
  """
  # Note that the shapes of the placeholders match the shapes of the full
  # image and label tensors, except the first dimension is now batch_size
  # rather than the full size of the train or test data sets.
  images_placeholder = tf.placeholder(tf.float32, shape=(batch_size,
                                                         c3d_model.NUM_FRAMES_PER_CLIP,
                                                         c3d_model.CROP_SIZE,
                                                         c3d_model.CROP_SIZE,
                                                         c3d_model.CHANNELS))
  labels_placeholder = tf.placeholder(tf.int64, shape=(batch_size))
  return images_placeholder, labels_placeholder


def average_gradients(tower_grads):
  average_grads = []
  for grad_and_vars in zip(*tower_grads):
    grads = []
    for g, _ in grad_and_vars:
      expanded_g = tf.expand_dims(g, 0)
      grads.append(expanded_g)
    grad = tf.concat(0, grads)
    grad = tf.reduce_mean(grad, 0)
    v = grad_and_vars[0][1]
    grad_and_var = (grad, v)
    average_grads.append(grad_and_var)
  return average_grads


def tower_loss(name_scope, logit, labels):
  cross_entropy_mean = tf.reduce_mean(
                  tf.nn.sparse_softmax_cross_entropy_with_logits(logit, labels)
                  )
  tf.summary.scalar(
                  name_scope + 'cross entropy',
                  cross_entropy_mean
                  )
  weight_decay_loss = tf.add_n(tf.get_collection('losses', name_scope))
  tf.summary.scalar(name_scope + 'weight decay loss', weight_decay_loss)
  tf.add_to_collection('losses', cross_entropy_mean)
  losses = tf.get_collection('losses', name_scope)

  # Calculate the total loss for the current tower.
  total_loss = tf.add_n(losses, name='total_loss')
  tf.summary.scalar(name_scope + 'total loss', total_loss)

  # Compute the moving average of all individual losses and the total loss.
  loss_averages = tf.train.ExponentialMovingAverage(0.99, name='loss')
  loss_averages_op = loss_averages.apply(losses + [total_loss])
  with tf.control_dependencies([loss_averages_op]):
    total_loss = tf.identity(total_loss)
  return total_loss

def tower_acc(logit, labels):
  correct_pred = tf.equal(tf.argmax(logit, 1), labels)
  accuracy = tf.reduce_mean(tf.cast(correct_pred, tf.float32))
  return accuracy

def _variable_on_cpu(name, shape, initializer):
  with tf.device('/cpu:0'):
    var = tf.get_variable(name, shape, initializer=initializer)
  return var

def _variable_with_weight_decay(name, shape, wd):
  var = _variable_on_cpu(name, shape, tf.contrib.layers.xavier_initializer())
  if wd is not None:
    weight_decay = tf.mul(tf.nn.l2_loss(var), wd, name='weight_loss')
    tf.add_to_collection('losses', weight_decay)
  return var


def run_training():
  # Get the sets of images and labels for training, validation, and
  # Tell TensorFlow that the model will be built into the default Graph.

  # Create model directory
  if not os.path.exists(FLAGS.train_dir):
      os.makedirs(FLAGS.train_dir)
  use_pretrained_model = False
  model_filename = "./sports1m_finetuning_ucf101.model"

  with tf.Graph().as_default():
    #Create a variable to count the number of train() calls. This equals the
    # number of batches processed * FLAGS.num_gpus.
    global_step = tf.get_variable(
        'global_step', [],
        initializer=tf.constant_initializer(0), trainable=False)

    # Calculate the learning rate schedule.
    num_batches_per_epoch = (c3d_model.NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN /
                              FLAGS.batch_size)
    decay_steps = int(num_batches_per_epoch * c3d_model.NUM_EPOCHS_PER_DECAY)

    # Decay the learning rate exponentially based on the number of steps.
    lr = tf.train.exponential_decay(c3d_model.INITIAL_LEARNING_RATE,
                                    global_step,
                                    decay_steps,
                                    c3d_model.LEARNING_RATE_DECAY_FACTOR,
                                    staircase=True)

    images_placeholder, labels_placeholder = placeholder_inputs(
                    FLAGS.batch_size * FALGS.gpu_num)
    tower_grads1 = []
    tower_grads2 = []
    logits = []
    opt1 = tf.train.AdamOptimizer(lr)
    opt2 = tf.train.AdamOptimizer(lr)
    for gpu_index in xrange(FALGS.gpu_num):
      with tf.device('/gpu:%d' % gpu_index):
        with tf.name_scope('%s_%d' % ('tower', gpu_index)) as scope:
          with tf.variable_scope('var_name') as var_scope:
            weights = {
              'wc1': _variable_with_weight_decay('wc1', [3, 3, 3, c3d_model.CHANNELS, 64], 0.0005),
              'wc2': _variable_with_weight_decay('wc2', [3, 3, 3, 64, 128], 0.0005),
              'wc3a': _variable_with_weight_decay('wc3a', [3, 3, 3, 128, 256], 0.0005),
              'wc3b': _variable_with_weight_decay('wc3b', [3, 3, 3, 256, 256], 0.0005),
              'wc4a': _variable_with_weight_decay('wc4a', [3, 3, 3, 256, 512], 0.0005),
              'wc4b': _variable_with_weight_decay('wc4b', [3, 3, 3, 512, 512], 0.0005),
              'wc5a': _variable_with_weight_decay('wc5a', [3, 3, 3, 512, 512], 0.0005),
              'wc5b': _variable_with_weight_decay('wc5b', [3, 3, 3, 512, 512], 0.0005),
              'wd1': _variable_with_weight_decay('wd1', [8192, 4096], 0.0005),
              'wd2': _variable_with_weight_decay('wd2', [4096, 4096], 0.0005),
              'out': _variable_with_weight_decay('wout', [4096, c3d_model.NUM_CLASSES], 0.0005)
              }
            biases = {
              'bc1': _variable_with_weight_decay('bc1', [64], 0.000),
              'bc2': _variable_with_weight_decay('bc2', [128], 0.000),
              'bc3a': _variable_with_weight_decay('bc3a', [256], 0.000),
              'bc3b': _variable_with_weight_decay('bc3b', [256], 0.000),
              'bc4a': _variable_with_weight_decay('bc4a', [512], 0.000),
              'bc4b': _variable_with_weight_decay('bc4b', [512], 0.000),
              'bc5a': _variable_with_weight_decay('bc5a', [512], 0.000),
              'bc5b': _variable_with_weight_decay('bc5b', [512], 0.000),
              'bd1': _variable_with_weight_decay('bd1', [4096], 0.000),
              'bd2': _variable_with_weight_decay('bd2', [4096], 0.000),
              'out': _variable_with_weight_decay('bout', [c3d_model.NUM_CLASSES], 0.000),
              }
          varlist1 = weights.values()
          varlist2 = biases.values()
          logit = c3d_model.inference_c3d(
                          images_placeholder[gpu_index * FLAGS.batch_size:(gpu_index + 1) * FLAGS.batch_size,:,:,:,:],
                          0.5,
                          FLAGS.batch_size,
                          weights,
                          biases
                          )
          loss = tower_loss(
                          scope,
                          logit,
                          labels_placeholder[gpu_index * FLAGS.batch_size:(gpu_index + 1) * FLAGS.batch_size]
                          )
          grads1 = opt1.compute_gradients(loss, varlist1)
          grads2 = opt2.compute_gradients(loss, varlist2)
          tower_grads1.append(grads1)
          tower_grads2.append(grads2)
          logits.append(logit)
          tf.get_variable_scope().reuse_variables()
    logits = tf.concat(0, logits)
    accuracy = tower_acc(logits, labels_placeholder)
    tf.summary.scalar('accuracy', accuracy)
    grads1 = average_gradients(tower_grads1)
    grads2 = average_gradients(tower_grads2)
    apply_gradient_op1 = opt1.apply_gradients(grads1)
    apply_gradient_op2 = opt2.apply_gradients(grads2, global_step=global_step)
    variable_averages = tf.train.ExponentialMovingAverage(c3d_model.MOVING_AVERAGE_DECAY)
    variables_averages_op = variable_averages.apply(tf.trainable_variables())
    train_op = tf.group(apply_gradient_op1, apply_gradient_op2, variables_averages_op)
    null_op = tf.no_op()

    # Create a saver for writing training checkpoints.
    saver = tf.train.Saver(weights.values() + biases.values())
    init = tf.initialize_all_variables()

    # Create a session for running Ops on the Graph.
    sess = tf.Session(
                    config=tf.ConfigProto(
                                    allow_soft_placement=True,
                                    log_device_placement=True
                                    )
                    )
    sess.run(init)
    if os.path.isfile(model_filename) and use_pretrained_model:
      saver.restore(sess, model_filename)

    # Create summary writter
    merged = tf.merge_all_summaries()
    train_writer = tf.train.SummaryWriter('./visual_logs/train', sess.graph)
    test_writer = tf.train.SummaryWriter('./visual_logs/test', sess.graph)
    for step in xrange(FLAGS.max_steps):
      start_time = time.time()
      train_images, train_labels, _, _, _ = input_data.read_clip_and_label(
                      filename='list/train.list',
                      batch_size=FLAGS.batch_size * FALGS.gpu_num,
                      num_frames_per_clip=c3d_model.NUM_FRAMES_PER_CLIP,
                      crop_size=c3d_model.CROP_SIZE,
                      shuffle=True
                      )
      sess.run(train_op, feed_dict={
                      images_placeholder: train_images,
                      labels_placeholder: train_labels
                      })
      duration = time.time() - start_time
      print('Step %d: %.3f sec' % (step, duration))

      # Save a checkpoint and evaluate the model periodically.
      if (step) % 10 == 0 or (step + 1) == FLAGS.max_steps:
        saver.save(sess, os.path.join(FLAGS.train_dir, 'c3d_model'), global_step=step)
        print('Training Data Eval:')
        summary, acc = sess.run(
                        [merged, accuracy],
                        feed_dict={
                                  images_placeholder: train_images,
                                  labels_placeholder: train_labels
                                  })
        print ("accuracy: " + "{:.5f}".format(acc))
        train_writer.add_summary(summary, step)
        print('Validation Data Eval:')
        val_images, val_labels, _, _, _ = input_data.read_clip_and_label(
                        filename='list/test.list',
                        batch_size=FLAGS.batch_size * FALGS.gpu_num,
                        num_frames_per_clip=c3d_model.NUM_FRAMES_PER_CLIP,
                        crop_size=c3d_model.CROP_SIZE,
                        shuffle=True
                        )
        summary, acc = sess.run(
                        [merged, accuracy],
                        feed_dict={
                                  images_placeholder: val_images,
                                  labels_placeholder: val_labels
                                  })
        print ("accuracy: " + "{:.5f}".format(acc))
        test_writer.add_summary(summary, step)
  print("done")

def main(_):
  run_training()

if __name__ == '__main__':
  tf.app.run()
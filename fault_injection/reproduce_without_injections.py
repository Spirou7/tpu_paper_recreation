# this is a custom file written by Michael Dalva to reproduce the training without injections

import tensorflow as tf
from local_tpu_resolver import LocalTPUClusterResolver

from models.resnet import resnet_18
from models.backward_resnet import backward_resnet_18
import config
from prepare_data import generate_datasets
import math
import os
import argparse
import numpy as np
from models.inject_utils import *
from injection import read_injection


tf.config.set_soft_device_placement(True)
tf.random.set_seed(123)


golden_grad_idx = {
    'resnet18': -2,
    }

class Replay():
    model = ''
    stage = ''
    fmodel = ''
    target_worker = -1
    target_layer = ''
    target_epoch = -1
    target_step =  -1
    inj_pos = []
    inj_values = []
    seed = 123


def parse_args():
    desc = "Tensorflow implementation of Resnet"
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('--file', type=str, help="Choose a csv file to replay")
    return parser.parse_args()


def get_model(m_name, seed):
    if m_name == 'resnet18':
        model = resnet_18(seed, m_name)
        model.build(input_shape=(None, config.image_height, config.image_width, config.channels))
        back_model = backward_resnet_18(m_name)

    return model, back_model

def main():
    args = parse_args()
    if args is None:
        exit()

    # TPU settings
    tpu_name = os.getenv('TPU_NAME')
    print("Attempting to connect to TPU:", tpu_name)
    resolver = LocalTPUClusterResolver()
    tf.tpu.experimental.initialize_tpu_system(resolver)

    strategy = tf.distribute.TPUStrategy(resolver)
    per_replica_batch_size = config.BATCH_SIZE // strategy.num_replicas_in_sync
    print("Finish TPU strategy setting!")


    rp = read_injection(args.file)
    rp.seed = 123

    # get the dataset
    train_dataset, valid_dataset, train_count, valid_count = generate_datasets(rp.seed)

    train_dataset = strategy.experimental_distribute_dataset(train_dataset)
    valid_dataset = strategy.experimental_distribute_dataset(valid_dataset)

    with strategy.scope():
        model, back_model = get_model(rp.model, rp.seed)
	# define loss and optimizer
        lr_schedule = tf.keras.optimizers.schedules.PolynomialDecay(
                initial_learning_rate=rp.learning_rate,
                decay_steps = 4000,
                end_learning_rate=0.0001)
        model.optimizer = tf.keras.optimizers.Adam(learning_rate=lr_schedule)
        train_loss = tf.keras.metrics.Mean(name='train_loss')
        train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='train_accuracy')
        epoch_loss = tf.keras.metrics.Mean(name='epoch_loss')
        epoch_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='epoch_accuracy')
        valid_loss = tf.keras.metrics.Mean(name='valid_loss')
        valid_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='valid_accuracy')

    @tf.function
    def train_step(iterator):
        def step_fn(inputs):
            images, labels = inputs
            with tf.GradientTape() as tape:
                outputs, _, _, l_outputs = model(images, training=True, inject=False)
                predictions = outputs['logits']
                loss = tf.keras.losses.sparse_categorical_crossentropy(labels, predictions)
                avg_loss = tf.nn.compute_average_loss(loss, global_batch_size=config.BATCH_SIZE)

            tvars = model.trainable_variables
            gradients = tape.gradient(avg_loss, tvars)
            model.optimizer.apply_gradients(grads_and_vars=list(zip(gradients, tvars)))

            train_loss.update_state(avg_loss * strategy.num_replicas_in_sync)
            train_accuracy.update_state(labels, predictions)
            epoch_loss.update_state(avg_loss * strategy.num_replicas_in_sync)
            epoch_accuracy.update_state(labels, predictions)

            return avg_loss

        return strategy.run(step_fn, args=(next(iterator),))


    @tf.function
    def fwrd_inj_train_step1(iter_inputs, inj_layer):
        def step1_fn(inputs):
            images, labels = inputs
            outputs, l_inputs, l_kernels, l_outputs = model(images, training=True, inject=False)
            predictions = outputs['logits']
            return l_inputs[inj_layer], l_kernels[inj_layer], l_outputs[inj_layer]
        return strategy.run(step1_fn, args=(iter_inputs,))

    @tf.function
    def fwrd_inj_train_step2(iter_inputs, inj_args, inj_flag):
        def step2_fn(inputs, inject):
            with tf.GradientTape() as tape:
                images, labels = inputs
                outputs, l_inputs, l_kernels, l_outputs = model(images, training=True, inject=inject, inj_args=inj_args)
                predictions = outputs['logits']
                grad_start = outputs['grad_start']
                loss = tf.keras.losses.sparse_categorical_crossentropy(labels, predictions)
                avg_loss = tf.nn.compute_average_loss(loss, global_batch_size=config.BATCH_SIZE)

            man_grad_start, golden_gradients = tape.gradient(avg_loss, [grad_start, model.trainable_variables])
            manual_gradients, _, _, _ = back_model(man_grad_start, l_inputs, l_kernels)

            gradients = manual_gradients + golden_gradients[golden_grad_idx[rp.model]:]
            model.optimizer.apply_gradients(list(zip(gradients, model.trainable_variables)))

            train_loss.update_state(avg_loss * strategy.num_replicas_in_sync)
            train_accuracy.update_state(labels, predictions)
            epoch_loss.update_state(avg_loss * strategy.num_replicas_in_sync)
            epoch_accuracy.update_state(labels, predictions)

            return avg_loss

        return strategy.run(step2_fn, args=(iter_inputs, inj_flag))

    @tf.function
    def bkwd_inj_train_step1(iter_inputs, inj_layer):
        def step1_fn(inputs):
            images, labels = inputs
            with tf.GradientTape() as tape:
                outputs, l_inputs, l_kernels, _ = model(images, training=True, inject=False)
                predictions = outputs['logits']
                grad_start = outputs['grad_start']
                loss = tf.keras.losses.sparse_categorical_crossentropy(labels, predictions)
                avg_loss = tf.nn.compute_average_loss(loss, global_batch_size=config.BATCH_SIZE)
            man_grad_start = tape.gradient(avg_loss, grad_start)
            _, bkwd_inputs, bkwd_kernels, bkwd_outputs = back_model(man_grad_start, l_inputs, l_kernels)
            return bkwd_inputs[inj_layer], bkwd_kernels[inj_layer], bkwd_outputs[inj_layer]

        return strategy.run(step1_fn, args=(iter_inputs,))

    @tf.function
    def bkwd_inj_train_step2(iter_inputs, inj_args, inj_flag):
        def step2_fn(inputs, inject):
            images, labels = inputs
            with tf.GradientTape() as tape:
                outputs, l_inputs, l_kernels, l_outputs = model(images, training=True, inject=False)
                predictions = outputs['logits']
                grad_start = outputs['grad_start']
                loss = tf.keras.losses.sparse_categorical_crossentropy(labels, predictions)
                avg_loss = tf.nn.compute_average_loss(loss, global_batch_size=config.BATCH_SIZE)
            man_grad_start, golden_gradients = tape.gradient(avg_loss, [grad_start, model.trainable_variables])
            manual_gradients, _, _, _ = back_model(man_grad_start, l_inputs, l_kernels, inject=inject, inj_args=inj_args)

            gradients = manual_gradients + golden_gradients[golden_grad_idx[rp.model]:]
            model.optimizer.apply_gradients(list(zip(gradients, model.trainable_variables)))

            train_loss.update_state(avg_loss * strategy.num_replicas_in_sync)
            train_accuracy.update_state(labels, predictions)
            epoch_loss.update_state(avg_loss * strategy.num_replicas_in_sync)
            epoch_accuracy.update_state(labels, predictions)
            return avg_loss

        return strategy.run(step2_fn, args=(iter_inputs, inj_flag))


    @tf.function
    def valid_step(iterator):
        def step_fn(inputs):
            images, labels = inputs
            outputs , _, _, _ = model(images, training=False)
            predictions = outputs['logits']
            v_loss = tf.keras.losses.sparse_categorical_crossentropy(labels, predictions)
            v_loss = tf.nn.compute_average_loss(v_loss, global_batch_size=config.BATCH_SIZE)
            valid_loss.update_state(v_loss)
            valid_accuracy.update_state(labels, predictions)
        return strategy.run(step_fn, args=(next(iterator),))

    steps_per_epoch = math.ceil(train_count / config.BATCH_SIZE)
    valid_steps_per_epoch = math.ceil(valid_count / config.VALID_BATCH_SIZE)
 
    target_epoch = rp.target_epoch
    target_step = rp.target_step

    train_recorder = open("without_injections_replay_{}.txt".format(args.file[args.file.rfind('/')+1:args.file.rfind('.')]), 'w')
    record(train_recorder, "Inject to epoch: {}\n".format(target_epoch))
    record(train_recorder, "Inject to step: {}\n".format(target_step))
    
    # removing this for now to see if i can still get the code to run
    '''
    ckpt_path = os.path.join(config.golden_model_dir, "epoch_{}".format(target_epoch - 1))
    record(train_recorder, "Load weights from {}\n".format(ckpt_path))
    model.load_weights(ckpt_path)
    '''


    start_epoch = target_epoch
    total_epochs = config.EPOCHS
    early_terminate = False
    epoch = start_epoch
    print("Starting training...")
    while epoch < total_epochs:
        print("Starting epoch: ", epoch)
        if early_terminate:
            print("Early terminate!")
            break
        train_loss.reset_states()
        train_accuracy.reset_states()
        epoch_loss.reset_states()
        epoch_accuracy.reset_states()

        valid_loss.reset_states()
        valid_accuracy.reset_states()
        step = 0

        train_iterator = iter(train_dataset)
        for step in range(steps_per_epoch):
            print("Step: ", step)
            train_loss.reset_states()
            train_accuracy.reset_states()
            if early_terminate:
                break

            # always perform the correct training step
            losses = train_step(train_iterator)

            record(train_recorder, "Epoch: {}/{}, step: {}/{}, loss: {:.5f}, accuracy: {:.5f}\n".format(epoch,
                             total_epochs,
                             step,
                             steps_per_epoch,
                             train_loss.result(),
                             train_accuracy.result()))

            if not np.isfinite(train_loss.result()):
                record(train_recorder, "Encounter NaN! Terminate training!\n")
                early_terminate = True

        if not early_terminate:
            valid_iterator = iter(valid_dataset)
            for _ in range(valid_steps_per_epoch):
                valid_step(valid_iterator)

            record(train_recorder, "End of epoch: {}/{}, train loss: {:.5f}, train accuracy: {:.5f}, "
                "valid loss: {:.5f}, valid accuracy: {:.5f}\n".format(epoch,
                             config.EPOCHS,
                             epoch_loss.result(),
                             epoch_accuracy.result(),
                             valid_loss.result(),
                             valid_accuracy.result()))

            # NaN value in validation
            if not np.isfinite(valid_loss.result()):
                record(train_recorder, "Encounter NaN! Terminate training!\n")

                early_terminate = True

        epoch += 1
    print("Training complete!")


if __name__ == '__main__':
    main()

import argparse
import glob
import json
import os
import pickle
import random as python_random
import uuid
from collections import Counter
from contextlib import redirect_stdout

import numpy as np
import pandas as pd
import tensorflow as tf
import matplotlib.pyplot as plt
import transformers

from evaluate import evaluate_roc, evaluate_topk


def set_seed():
    # The below is necessary for starting Numpy generated random numbers
    # in a well-defined initial state.
    np.random.seed(123)

    # The below is necessary for starting core Python generated random numbers
    # in a well-defined state.
    python_random.seed(123)

    # The below set_seed() will make random number generation
    # in the TensorFlow backend have a well-defined initial state.
    # For further details, see
    # https://www.tensorflow.org/api_docs/python/tf/random/set_seed
    tf.random.set_seed(1234)


def arg_parser():
    parser = argparse.ArgumentParser()

    # Data args
    parser.add_argument('--lag', type=int, default=None, help='')
    parser.add_argument('--lags', type=int, nargs='+', default=None, help='')
    parser.add_argument('--signal-pickle', type=str, required=True, help='')
    parser.add_argument('--label-pickle', type=str, required=True, help='')
    parser.add_argument('--half-window', type=int, default=16, help='')

    # Training args
    parser.add_argument('--lr',
                        type=float,
                        default=0.01,
                        help='Optimizer learning rate.')
    parser.add_argument('--batch-size',
                        type=int,
                        default=512,
                        help='Integer or None. Number of samples per '
                        'gradient update.')
    parser.add_argument('--fine-epochs',
                        type=int,
                        default=1000,
                        help='Integer. Number of epochs to train the model. '
                        'An epoch is an iteration over the entire x and '
                        'y data provided.')
    parser.add_argument('--patience',
                        type=int,
                        default=150,
                        help='Number of epochs with no improvement after '
                        'which training will be stopped.')
    parser.add_argument('--lm-head',
                        action='store_true',
                        help='NotImplementedError')
    parser.add_argument('--ensemble',
                        action='store_true',
                        help='Use the trained models to create an ensemble. '
                        'No training is performed.')
    parser.add_argument('--n-weight-avg', type=int, default=0)

    # Model definition
    parser.add_argument('--conv-filters',
                        type=int,
                        default=128,
                        help='Number of convolutional filters in the model.')
    parser.add_argument('--reg',
                        type=float,
                        default=0.35,
                        help='Float. L2 regularization factor for '
                        'convolutional layers.')
    parser.add_argument('--reg-head',
                        type=float,
                        default=0,
                        help='Float. L2 regularization factor for dense head.')
    parser.add_argument('--dropout',
                        type=float,
                        default=0.2,
                        help='Float between 0 and 1. Fraction of the input '
                        'units to drop.')

    # Other args
    parser.add_argument('--model',
                        type=str,
                        default='default-out',
                        help='Name of output directory.')
    parser.add_argument('--seed', type=int, default=None, help='Random seed.')
    parser.add_argument('--verbose',
                        type=int,
                        default=2,
                        help='0, 1, or 2. Verbosity mode. 0 = silent, '
                        '1 = progress bar, 2 = one line per epoch.')

    args = parser.parse_args()

    if args.lag is None:
        if os.environ.get('SLURM_ARRAY_TASK_ID') is not None:
            idx = int(os.environ.get('SLURM_ARRAY_TASK_ID'))
            assert len(args.lags) > 0
            assert idx <= len(args.lags)

            args.lag = args.lags[idx - 1]
            print(f'Using slurm array lag: {args.lag}')
        else:
            args.lag = 0  # default

    return args


def load_pickles(args):
    with open(args.signal_pickle, 'rb') as fh:
        signal_d = pickle.load(fh)

    with open(args.label_pickle, 'rb') as fh:
        label_folds = pickle.load(fh)

    print('Signals pickle info')
    for key in signal_d.keys():
        print(f'key: {key}, \t '
              f'type: {type(signal_d[key])}, \t '
              f'shape: {len(signal_d[key])}')

    assert signal_d['binned_signal'].shape[0] == signal_d['bin_stitch_index'][
        -1], 'Error: Incorrect Stitching'
    assert signal_d['binned_signal'].shape[1] == len(
        signal_d['electrodes']), 'Error: Incorrect number of electrodes'

    signals = signal_d['binned_signal']
    stitch_index = signal_d['bin_stitch_index']
    stitch_index.insert(0, 0)

    # The first 64 electrodes correspond to the hemisphere of interest
    # signals = signals[:, :64]
    # print(signals.shape)

    # The labels have been stemmed using Porter Stemming Algorithm

    return signals, stitch_index, label_folds


def pitom(input_shapes, n_classes):
    '''
    pitom1: [(128,9), (128,9), ('max', 2), (128, 4)]; DP 0.1 LR 2e-5
    pitom2: [(128,9), ('max',2), (128,4)]; DP 0.1, REG .05; LR 1e-3
    input_shapes = (input_shape_cnn, input_shape_emb)
    '''

    desc = [(args.conv_filters, 3), ('max', 2), (args.conv_filters, 2)]

    input_cnn = tf.keras.Input(shape=input_shapes[0])

    prev_layer = input_cnn
    for filters, kernel_size in desc:
        if filters == 'max':
            prev_layer = tf.keras.layers.MaxPooling1D(
                pool_size=kernel_size, strides=None,
                padding='same')(prev_layer)
        else:
            # Add a convolution block
            prev_layer = tf.keras.layers.Conv1D(
                filters,
                kernel_size,
                strides=1,
                padding='valid',
                use_bias=False,
                kernel_regularizer=tf.keras.regularizers.l2(args.reg),
                kernel_initializer='glorot_normal')(prev_layer)
            prev_layer = tf.keras.layers.Activation('relu')(prev_layer)
            prev_layer = tf.keras.layers.BatchNormalization()(prev_layer)
            prev_layer = tf.keras.layers.Dropout(args.dropout)(prev_layer)

    # Add final conv block
    prev_layer = tf.keras.layers.LocallyConnected1D(
        filters=args.conv_filters,
        kernel_size=2,
        strides=1,
        padding='valid',
        kernel_regularizer=tf.keras.regularizers.l2(args.reg),
        kernel_initializer='glorot_normal')(prev_layer)
    prev_layer = tf.keras.layers.BatchNormalization()(prev_layer)
    prev_layer = tf.keras.layers.Activation('relu')(prev_layer)

    cnn_features = tf.keras.layers.GlobalMaxPooling1D()(prev_layer)

    output = cnn_features
    if n_classes is not None:
        output = tf.keras.layers.LayerNormalization()(tf.keras.layers.Dense(
            units=n_classes,
            kernel_regularizer=tf.keras.regularizers.l2(args.reg_head),
            activation='tanh')(cnn_features))

    model = tf.keras.Model(inputs=input_cnn, outputs=output)
    return model


class WeightAverager(tf.keras.callbacks.Callback):
    """Averages model weights across training trajectory, starting at
    designated epoch."""
    def __init__(self, epoch_count, patience):
        super(WeightAverager, self).__init__()
        self.epoch_count = min(epoch_count, 2 * patience)
        self.weights = []
        self.patience = patience

    def on_train_begin(self, logs=None):
        print('Weight averager over last {} epochs.'.format(self.epoch_count))

    def on_epoch_end(self, epoch, logs=None):
        if len(self.weights) and len(
                self.weights) == self.patience + self.epoch_count / 2:
            self.weights.pop(0)
        self.weights.append(self.model.get_weights())

    def on_train_end(self, logs=None):
        if self.weights:
            self.best_weights = np.asarray(self.model.get_weights())
            w = 0
            p = 0
            for p, nw in enumerate(self.weights):
                w = (w * p + np.asarray(nw)) / (p + 1)
                if p >= self.epoch_count:
                    break
            self.model.set_weights(w)
            print('Averaged {} weights.'.format(p + 1))


# Define language model decoder
def language_decoder(args):
    lang_model = transformers.TFBertForMaskedLM.from_pretrained(
        args.model_name, cache_dir='/scratch/gpfs/zzada/cache-tf')
    d_size = lang_model.config.hidden_size
    v_size = lang_model.config.vocab_size

    lang_decoder = lang_model.mlm
    lang_decoder.trainable = False

    inputs = tf.keras.Input((d_size, ))
    x = tf.keras.layers.Reshape((1, d_size))(inputs)
    x = lang_decoder(x)
    x = tf.keras.layers.Reshape((v_size, ))(x)
    # x = Lambda(lambda z: tf.gather(z, vocab_indices, axis=-1))(x)
    x = tf.keras.layers.Activation('softmax')(x)
    lm_decoder = tf.keras.Model(inputs=inputs, outputs=x)
    lm_decoder.summary()
    return lm_decoder


def get_decoder():
    if args.lm_head:
        return language_decoder()
    else:
        return tf.keras.layers.Dense(
            n_classes,
            kernel_regularizer=tf.keras.regularizers.l2(args.reg_head))


def extract_signal_from_fold(examples, stitch_index, args):

    lag_in_bin_dim = args.lag // 32
    half_window = args.half_window  # // 32

    x, w = [], []
    for label in examples:
        bin_index = label['onset'] // 32
        bin_rank = (np.array(stitch_index) < bin_index).nonzero()[0][-1]
        bin_start = stitch_index[bin_rank]
        bin_stop = stitch_index[bin_rank + 1]

        left_edge = bin_index + lag_in_bin_dim - half_window
        right_edge = bin_index + lag_in_bin_dim + half_window

        if (left_edge < bin_start) or (right_edge > bin_stop):
            continue
        else:
            x.append(signals[left_edge:right_edge, :])
            w.append(label['word'])

    x = np.stack(x, axis=0)
    w = np.array(w)

    return x, w


if __name__ == '__main__':

    set_seed()
    args = arg_parser()

    # Set up save directory
    taskID = os.environ.get('SLURM_ARRAY_TASK_ID')
    jobID = os.environ.get('SLURM_ARRAY_JOB_ID')
    nonce = f'{jobID}-' if jobID is not None else ''
    nonce += f'{taskID}-' if taskID is not None else ''
    nonce += uuid.uuid4().hex[:8]
    nonce = 'ensemble' if args.ensemble else nonce

    save_dir = os.path.join('results', args.model, str(args.lag), nonce)
    os.makedirs(save_dir, exist_ok=True)

    args.save_dir = save_dir
    args.task_id = taskID
    args.job_id = jobID
    print(args)

    with open(os.path.join(save_dir, 'args.json'), 'w') as fp:
        json.dump(vars(args), fp, indent=4)

    signals, stitch_index, label_folds = load_pickles(args)
    histories = []
    fold_results = []

    # TODO - do all folds.
    for i in range(5):
        print(f'Running fold {i}')
        results = {}

        train_fold = [
            example for example in label_folds
            if example[f'fold{i}'] == 'train'
        ]
        dev_fold = [
            example for example in label_folds if example[f'fold{i}'] == 'dev'
        ]
        test_fold = [
            example for example in label_folds if example[f'fold{i}'] == 'test'
        ]

        # Decoding starts here
        x_train, w_train = extract_signal_from_fold(train_fold, stitch_index,
                                                    args)
        x_dev, w_dev = extract_signal_from_fold(dev_fold, stitch_index, args)
        x_test, w_test = extract_signal_from_fold(test_fold, stitch_index,
                                                  args)

        # Determine indexing
        word2index = {
            w: j
            for j, w in enumerate(sorted(set(w_train.tolist())))
        }
        index2word = {j: word for word, j in word2index.items()}

        y_train = np.array([word2index[w] for w in w_train])
        y_dev = np.array([word2index[w] for w in w_dev])
        y_test = np.array([word2index[w] for w in w_test])

        n_classes = np.unique(y_train).size

        print('X train, dev, test:', x_train.shape, x_dev.shape, x_test.shape)
        print('Y train, dev, test:', y_train.shape, y_dev.shape, y_test.shape)
        print('W train, dev, test:', w_train.shape, w_dev.shape, w_test.shape)
        print('n_classes:', n_classes,
              np.unique(y_dev).size,
              np.unique(y_test).size)

        results['n_train'] = x_train.shape[0]
        results['n_dev'] = x_dev.shape[0]
        results['n_test'] = x_test.shape[0]
        results['n_classes'] = np.unique(y_train).size
        results['n_classes_dev'] = np.unique(y_dev).size
        results['n_classes_test'] = np.unique(y_test).size

        model = pitom([x_train.shape[1:]], n_classes=None)
        optimizer = tf.keras.optimizers.Adam(lr=args.lr)
        model.compile(loss='mse',
                      optimizer=optimizer,
                      metrics=[tf.keras.metrics.CosineSimilarity()])

        # model.summary()

        # -------------------------------------------------------------------------
        # >> Classification training
        # -------------------------------------------------------------------------
        train_histories = []
        models = []  # len(models) > 1 when using ensemble
        loaded_model = False  # TODO - make an arg appropriately

        # Add the decoder, LM head or just a new layer
        if args.fine_epochs > 0 and not args.ensemble:
            model2 = tf.keras.Model(inputs=model.input,
                                    outputs=get_decoder()(model.output))
            model2.compile(
                loss=tf.keras.losses.CategoricalCrossentropy(from_logits=True),
                optimizer=optimizer,
                metrics=[
                    tf.keras.metrics.CategoricalAccuracy(name='accuracy'),
                ])

            with open(os.path.join(save_dir, 'model2-summary.txt'), 'w') as fp:
                with redirect_stdout(fp):
                    model2.summary()

            callbacks = []
            if args.patience > 0:
                stopper = tf.keras.callbacks.EarlyStopping(
                    monitor='val_accuracy',
                    mode='max',
                    patience=args.patience,
                    restore_best_weights=True,
                    verbose=args.verbose)
                callbacks.append(stopper)

            if args.n_weight_avg > 0:
                averager = WeightAverager(args.n_weight_avg, args.patience)
                callbacks.append(averager)

            history = model2.fit(
                x=x_train,
                y=tf.keras.utils.to_categorical(y_train, n_classes),
                epochs=args.fine_epochs,
                batch_size=args.batch_size,
                validation_data=[
                    x_dev,
                    tf.keras.utils.to_categorical(y_dev, n_classes)
                ],
                callbacks=[stopper],
                verbose=args.verbose)

            model2.save(os.path.join(save_dir, f'model2-fold{i}.h5'))
            models.append(model2)

            train_histories.append(history.history)

            # Store final value of each dev metric, then test metrics
            results.update(
                {k: float(v[-1])
                 for k, v in train_histories[-1].items()})
        elif args.ensemble:
            prev_dir = os.path.dirname(save_dir)
            for fn in glob.glob(f'{prev_dir}/*/model2-fold{i}.h5'):
                if os.path.isfile(fn):
                    try:
                        models.append(tf.keras.models.load_model(fn))
                        print(f'Loaded {fn}')
                    except Exception as e:
                        print(f'Problem loading model: {e}')
            assert len(models) > 0, f'No trained models found: {prev_dir}'
            results['n_models'] = len(models)
        else:
            # NOTE - this should not be reached given how we do nonce save_dir
            trained_model_fn = os.path.join(save_dir, f'model2-fold{i}.h5')
            if os.path.isfile(trained_model_fn):
                print('Loading model!')
                loaded_model = True
                model2 = tf.keras.models.load_model(trained_model_fn)
                models.append(model2)
            else:
                assert False, 'No trained model to load.'

        # -------------------------------------------------------------------------
        # >> Classification evaluation
        # -------------------------------------------------------------------------

        res, res2 = {}, {}
        if args.lm_head or args.fine_epochs > 0 or loaded_model or args.ensemble:

            w_train_freq = Counter(w_train)
            y_test_1hot = tf.keras.utils.to_categorical(y_test, n_classes)

            # Evaluate using tensorflow metrics
            if len(models) == 1:
                model = models[0]
                eval_test = model2.evaluate(x_test, y_test_1hot)
                results.update({
                    metric: float(value)
                    for metric, value in zip(model2.metrics_names, eval_test)
                })

            # Get model or ensemble predictions
            if len(models) == 1:
                predictions = model2.predict(x_test)
            elif len(models) > 1:
                predictions = np.zeros((len(models), len(x_test), n_classes))
                for j, model in enumerate(models):
                    predictions[j] = model.predict(x_test)
                predictions = np.average(predictions, axis=0)

            assert n_classes == predictions.shape[1]

            res = evaluate_topk(predictions,
                                y_test_1hot,
                                index2word,
                                w_train_freq,
                                save_dir,
                                prefix='test_',
                                suffix=f'ds_test-fold_{i}')
            results.update(res)

            res2 = evaluate_roc(predictions,
                                y_test_1hot,
                                index2word,
                                w_train_freq,
                                save_dir,
                                prefix='test_',
                                suffix=f'ds_test-fold_{i}',
                                title=args.model)
            results.update(res2)

        fold_results.append(results)
        print(
            json.dumps(
                {
                    k: v
                    for k, v in results.items()
                    if not isinstance(v, pd.DataFrame)
                },
                indent=2))

    # -------------------------------------------------------------------------
    # >> Plot training metrics
    # ------------------------------------------------------------------------

    if len(train_histories) > 0:
        for metric in train_histories[0]:
            if 'val' in metric:
                continue

            plt.figure()
            for history in train_histories:
                # Plot train
                val_train = history[metric][-1]
                plt.plot(history[metric], label=f'Training: {val_train:.3f}')

                # Plot val
                val_metric = f'val_{metric}'
                if val_metric in history:
                    val_dev = history[val_metric][-1]
                    plt.plot(history[val_metric], label=f'val: {val_dev:.3f}')

            plt.title(f'{args.model}')
            plt.xlabel('Epoch')
            plt.ylabel(f'{metric}')
            plt.tight_layout()
            plt.legend()
            plt.savefig(os.path.join(save_dir, f'epoch-{metric}.png'))
            plt.close()

    # -------------------------------------------------------------------------
    # >> Save results
    # -------------------------------------------------------------------------

    # Save all metrics
    results = {}
    for metric in fold_results[0]:
        values = [tr[metric] for tr in fold_results]
        agg = pd.concat if isinstance(values[0], pd.DataFrame) else np.mean
        results[f'avg_{metric}'] = agg(values)

    # Save all dataframes. TODO - there's some hard coded stuff in here that
    # needs to change. This whole df feature is probably over engineered.
    dfs = {k: df for k, df in results.items() if isinstance(df, pd.DataFrame)}
    dfs['avg_test_topk_guesses_df'].to_csv(
        os.path.join(save_dir, 'avg_test_topk_guesses_df.csv'))
    merged = dfs['avg_test_topk_df'].merge(dfs['avg_test_rocauc_df'])
    merged = merged.set_index(['word', 'ds', 'fold'])
    merged.to_csv(os.path.join(save_dir, 'avg_test_topk_rocaauc_df.csv'))

    # Remove all non-serializable objects
    for key in [
            key for key, value in results.items()
            if isinstance(value, pd.DataFrame)
    ]:
        del results[key]
    for result in fold_results:
        for key in [
                key for key, value in result.items()
                if isinstance(value, pd.DataFrame)
        ]:
            del result[key]

    # Write out everything
    print(json.dumps(results, indent=2))

    results['runs'] = fold_results
    results['args'] = vars(args)

    with open(os.path.join(save_dir, 'results.json'), 'w') as fp:
        json.dump(results, fp, indent=4)

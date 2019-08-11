import argparse
import os
import datetime
from keras.models import load_model
from keras import backend as K
from keras.utils import np_utils
from data import load_isic_data, train_validation_split, compute_class_weight_dict, get_dataframe_from_img_folder
from vanilla_classifier import VanillaClassifier
from transfer_learn_classifier import TransferLearnClassifier
from metrics import balanced_accuracy
from base_model_param import get_transfer_model_param_map
from lesion_classifier import LesionClassifier
from odin import compute_baseline_softmax_scores, compute_odin_softmax_scores, compute_out_of_distribution_score
from utils import ensemble_predictions

def main():
    parser = argparse.ArgumentParser(description='ISIC-2019 Skin Lesion Classifiers')
    parser.add_argument('data', metavar='DIR', help='path to data foler')
    parser.add_argument('--batchsize', type=int, help='Batch size (default: %(default)s)', default=32)
    parser.add_argument('--maxqueuesize', type=int, help='Maximum size for the generator queue (default: %(default)s)', default=10)
    parser.add_argument('--epoch', type=int, help='Number of epochs (default: %(default)s)', default=100)
    parser.add_argument('--model', dest='models', nargs='*', choices=['Vanilla', 'DenseNet201', 'Xception', 'ResNeXt50', 'NASNetLarge', 'InceptionResNetV2'], help='Models')
    parser.add_argument('--autoshutdown', dest='autoshutdown', action='store_true', help='Automatically shutdown the computer after everything is done')
    parser.add_argument('--training', dest='training', action='store_true', help='Train models')
    parser.add_argument('--predval', dest='predval', action='store_true', help='Predict validation set')
    parser.add_argument('--predtest', dest='predtest', action='store_true', help='Predict test data containing 8238 JPEG images of skin lesions')
    parser.add_argument('--predresultfolder', help='Name of the prediction result folder (default: %(default)s)', default='predict_results')
    parser.add_argument('--modelfolder', help='Name of the model folder (default: %(default)s)', default='models')
    parser.add_argument('--odinscore', dest='odinscore', action='store_true', help='Computing Baseline and ODIN softmax scores')
    args = parser.parse_args()
    print(args)

    # Write command to a file
    with open('Cmd_History.txt', 'a') as f:
        f.write("{}\t{}\n".format(str(datetime.datetime.utcnow()), str(args)))

    data_folder = args.data
    pred_result_folder = args.predresultfolder
    os.makedirs(pred_result_folder, exist_ok=True)
    model_folder = args.modelfolder
    softmax_score_folder = 'softmax_scores'
    os.makedirs(softmax_score_folder, exist_ok=True)
    batch_size = args.batchsize
    max_queue_size = args.maxqueuesize
    epoch_num = args.epoch

    derm_image_folder = os.path.join(data_folder, 'ISIC_2019_Training_Input')
    ground_truth_file = os.path.join(data_folder, 'ISIC_2019_Training_GroundTruth.csv')
    df_ground_truth, known_category_names, unknown_category_name = load_isic_data(derm_image_folder, ground_truth_file)
    known_category_num = len(known_category_names)
    df_train, df_val = train_validation_split(df_ground_truth)
    class_weight_dict, _ = compute_class_weight_dict(df_train)

    test_image_folder = os.path.join(data_folder, 'ISIC_2019_Test_Input')

    out_dist_image_folder = os.path.join(data_folder, 'Out_Distribution')
    out_dist_pred_result_folder = 'out_dist_predict_results'

    # Models used to predict validation set
    models_to_predict_val = []
    workers = os.cpu_count()

    # Train Vanilla CNN
    if args.models is not None and 'Vanilla' in args.models:
        input_size_vanilla = (224, 224)
        if args.training:
            train_vanilla(df_train, df_val, known_category_num, class_weight_dict, batch_size, max_queue_size, epoch_num, input_size_vanilla, model_folder)
        models_to_predict_val.append({'model_name': 'Vanilla',
                                      'input_size': input_size_vanilla,
                                      'preprocessing_function': VanillaClassifier.preprocess_input})
    
    transfer_models = args.models.copy()
    if 'Vanilla' in transfer_models:
        transfer_models.remove('Vanilla')
    
    # Train models by Transfer Learning
    if args.models is not None:
        model_param_map = get_transfer_model_param_map()
        base_model_params = [model_param_map[x] for x in transfer_models]
        if args.training:
            train_transfer_learning(base_model_params, df_train, df_val, known_category_num, class_weight_dict, batch_size, max_queue_size, epoch_num, model_folder)
        for base_model_param in base_model_params:
            models_to_predict_val.append({'model_name': base_model_param.class_name,
                                        'input_size': base_model_param.input_size,
                                        'preprocessing_function': base_model_param.preprocessing_func})

    # Predict validation set
    if args.predval:
        # Save Ground Truth of validation set
        val_ground_truth_file_path = os.path.join(pred_result_folder, 'Validation_Set_GroundTruth.csv')
        df_val.drop(columns=['path', 'category']).to_csv(path_or_buf=val_ground_truth_file_path, index=False)
        print("Save \"{}\"".format(val_ground_truth_file_path))

        postfixes = ['best_balanced_acc', 'best_loss', 'latest']
        for postfix in postfixes:
            for m in models_to_predict_val:
                model_filepath = os.path.join(model_folder, "{}_{}.hdf5".format(m['model_name'], postfix))
                if os.path.exists(model_filepath):
                    print("===== Predict validation set using \"{}_{}\" model =====".format(m['model_name'], postfix))
                    model = load_model(filepath=model_filepath, custom_objects={'balanced_accuracy': balanced_accuracy(known_category_num)})
                    LesionClassifier.predict_dataframe(model=model, df=df_val,
                                                       category_names=known_category_names,
                                                       augmentation_pipeline=LesionClassifier.create_aug_pipeline_val(m['input_size']),
                                                       preprocessing_function=m['preprocessing_function'],
                                                       batch_size=batch_size,
                                                       workers=workers,
                                                       softmax_save_file_name=os.path.join(pred_result_folder, "{}_{}.csv").format(m['model_name'], postfix),
                                                       logit_save_file_name=os.path.join(pred_result_folder, "{}_{}_logit.csv").format(m['model_name'], postfix))
                    del model
                    K.clear_session()
                else:
                    print("\"{}\" doesn't exist".format(model_filepath))

    # Predict Test Data
    if args.predtest:
        df_test = get_dataframe_from_img_folder(test_image_folder, has_path_col=True)
        df_test.drop(columns=['path']).to_csv(os.path.join(pred_result_folder, 'ISIC_2019_Test.csv'), index=False)
        postfix = 'best_balanced_acc'
        for m in models_to_predict_val:
            model_filepath = os.path.join(model_folder, "{}_{}.hdf5".format(m['model_name'], postfix))
            if os.path.exists(model_filepath):
                print("===== Predict test data using \"{}_{}\" model =====".format(m['model_name'], postfix))
                model = load_model(filepath=model_filepath, custom_objects={'balanced_accuracy': balanced_accuracy(known_category_num)})
                LesionClassifier.predict_dataframe(model=model, df=df_test,
                                                   category_names=known_category_names,
                                                   augmentation_pipeline=LesionClassifier.create_aug_pipeline_val(m['input_size']),
                                                   preprocessing_function=m['preprocessing_function'],
                                                   batch_size=batch_size,
                                                   workers=workers,
                                                   softmax_save_file_name=os.path.join(pred_result_folder, "{}_{}.csv").format(m['model_name'], postfix),
                                                   logit_save_file_name=os.path.join(pred_result_folder, "{}_{}_logit.csv").format(m['model_name'], postfix))
                del model
                K.clear_session()
            else:
                print("\"{}\" doesn't exist".format(model_filepath))

        # Ensemble Models' Predictions on Test Data
        df_ensemble = ensemble_predictions(result_folder=pred_result_folder, category_names=known_category_names, save_file=False,
                                           model_names=transfer_models, postfixes=[postfix]).drop(columns=['pred_category'])
        # Compute Out-of-Distribution scores
        df_score = compute_out_of_distribution_score(model_folder=model_folder, df=df_test, num_classes=known_category_num, batch_size=batch_size)
        # Merge ensemble predictions with out-of-Distribution scores
        df_ensemble[unknown_category_name] = df_score['out_dist_score']
        df_ensemble.to_csv(os.path.join(pred_result_folder, "Ensemble_{}.csv".format(postfix)), index=False)

    # Compute Baseline and ODIN Softmax Scores
    if args.odinscore:
        compute_baseline_softmax_scores(in_dist_pred_result_folder=pred_result_folder,
                                        out_dist_pred_result_folder=out_dist_pred_result_folder,
                                        softmax_score_folder=softmax_score_folder)
                                        
        compute_odin_softmax_scores(in_dist_pred_result_folder=pred_result_folder, in_dist_image_folder=derm_image_folder,
                                    out_dist_pred_result_folder=out_dist_pred_result_folder, out_dist_image_folder=out_dist_image_folder,
                                    model_folder=model_folder, softmax_score_folder=softmax_score_folder,
                                    num_classes=known_category_num, batch_size=batch_size)

    # Shutdown
    if args.autoshutdown:
        # Shutdown after 2 minutes
        os.system("sudo shutdown -h +2")


def train_vanilla(df_train, df_val, num_classes, class_weight_dict, batch_size, max_queue_size, epoch_num, input_size, model_folder):
    workers = os.cpu_count()

    classifier = VanillaClassifier(
        model_folder=model_folder,
        input_size=input_size,
        image_data_format=K.image_data_format(),
        num_classes=num_classes,
        batch_size=batch_size,
        max_queue_size=max_queue_size,
        class_weight=class_weight_dict,
        metrics=[balanced_accuracy(num_classes), 'accuracy'],
        image_paths_train=df_train['path'].tolist(),
        categories_train=np_utils.to_categorical(df_train['category'], num_classes=num_classes),
        image_paths_val=df_val['path'].tolist(),
        categories_val=np_utils.to_categorical(df_val['category'], num_classes=num_classes)
    )
    classifier.model.summary()
    print('Begin to train Vanilla CNN')
    classifier.train(epoch_num=epoch_num, workers=workers)
    del classifier
    K.clear_session()


def train_transfer_learning(base_model_params, df_train, df_val, num_classes, class_weight_dict, batch_size, max_queue_size, epoch_num, model_folder):
    workers = os.cpu_count()

    for model_param in base_model_params:
        classifier = TransferLearnClassifier(
            model_folder=model_folder,
            base_model_param=model_param,
            fc_layers=[512], # e.g. [512]
            num_classes=num_classes,
            dropout=0.3, # e.g. 0.3
            batch_size=batch_size,
            max_queue_size=max_queue_size,
            image_data_format=K.image_data_format(),
            metrics=[balanced_accuracy(num_classes), 'accuracy'],
            class_weight=class_weight_dict,
            image_paths_train=df_train['path'].tolist(),
            categories_train=np_utils.to_categorical(df_train['category'], num_classes=num_classes),
            image_paths_val=df_val['path'].tolist(),
            categories_val=np_utils.to_categorical(df_val['category'], num_classes=num_classes)
        )
        classifier.model.summary()
        print("Begin to train {}".format(model_param.class_name))
        classifier.train(epoch_num=epoch_num, workers=workers)
        del classifier
        K.clear_session()


if __name__ == '__main__':
    main()

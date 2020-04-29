import argparse
import os

from segmentation3d.core.seg_train import train


def main():

    os.environ['CUDA_VISIBLE_DEVICES'] = '4,5,6'

    long_description = "Training engine for 3d medical image segmentation"
    parser = argparse.ArgumentParser(description=long_description)

    parser.add_argument('-i', '--input',
                        default='./config/train_config.py',
                        help='configure file for medical image segmentation training.')
    args = parser.parse_args()

    train(args.input)


if __name__ == '__main__':
    main()

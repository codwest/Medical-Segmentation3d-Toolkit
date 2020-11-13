import os

from segmentation3d.core.seg_eval import cal_dsc_batch
from segmentation3d.core.seg_infer import read_test_txt, read_test_csv


def test_cal_dsc_batch():
  test_file = '/shenlab/lab_stor6/qinliu/CT_Dental/datasets/segmentation/test.txt'
  gt_folder = '/shenlab/lab_stor6/projects/CT_Dental/data'
  gt_name = 'seg.mha'
  seg_folder = '/shenlab/lab_stor6/qinliu/CT_Dental/results/seg_benchmark4'
  seg_name = 'seg.mha'
  result_file = '/shenlab/lab_stor6/qinliu/CT_Dental/results/seg_benchmark4/results.csv'

  if test_file.endswith('.txt'):
    file_list, case_list = read_test_txt(test_file)

  elif test_file.endswith('.csv'):
    file_list, case_list = read_test_csv(test_file)

  else:
    raise ValueError('Unsupported file')

  gt_files = []
  for case_name in file_list:
    gt_files.append(os.path.join(gt_folder, case_name, seg_name))

  seg_files = []
  for case_name in file_list:
    seg_files.append(os.path.join(seg_folder, case_name, seg_name))

  labels = [1,2]
  cal_dsc_batch(gt_files, seg_files, labels, 10, result_file)


if __name__ == '__main__':
  test_cal_dsc_batch()

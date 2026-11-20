"""Task-dependent paper extraction contracts shared by reading and experiment planning."""

COMMON = ('dataset', 'dataset_version', 'split', 'preprocessing', 'evaluation_split',
          'metrics', 'metric_scale', 'aggregation', 'input_spec', 'output_spec')
BY_TASK = {
    'forecasting': ('channels', 'sampling_frequency', 'input_length', 'output_length', 'target_columns'),
    'image_classification': ('image_resolution', 'input_channels', 'label_space', 'evaluation_transforms'),
    'object_detection': ('image_resolution', 'input_channels', 'label_space', 'box_format', 'iou_thresholds', 'evaluation_transforms'),
    'segmentation': ('image_resolution', 'input_channels', 'mask_resolution', 'label_space', 'ignore_label', 'evaluation_transforms'),
    'image_restoration': ('image_resolution', 'degradation', 'output_resolution', 'scale_factor', 'evaluation_transforms'),
    'other': (),
}

GUIDANCE = '''
先从论文任务定义、数据说明、实验设置和结果表判断输入是什么、模型输出什么、对什么真值评分。按任务抽取原文可以支持的字段。
共同记录数据集名称/版本、官方划分及实际评估子集、预处理、输入输出、指标/方向/单位/尺度、汇总及模型选择规则。
预测任务提取输入长度候选、各预测长度、变量与目标列、频率、划分、标准化和逆变换；将固定窗口、从候选取优、跨预测长度平均分别记录。
图像分类提取输入分辨率/通道、resize/crop、归一化、类别/标签和 Top-1/Top-5 等指标；检测提取框/类别、IoU 与 AP 定义，
分割提取掩码、类别、忽略标签和 mIoU/Dice 的聚合，复原提取退化模型、输入输出尺寸/倍率和 PSNR/SSIM 等评估细节。
其他任务按论文填写自己的输入、输出、标注、评测条件与任务专属字段。不适用的字段不填写；适用而原文未披露则记 not_reported。
逐个基线方法、数据集、实验设置与指标提取论文所报数字。每个结果卡保存原数值和精度、表/页/行列、方法列与指标表头、所在行、表注和原文证据。
把上述条件与具体数值绑定在同一结果卡的 setting/locator/quote 及对应协议记录，不混用不同表、不同尺度、不同任务或验证/测试分数。
用 literature.record_protocol 时可用 reported_baselines=[{method,result_index,header_excerpt_index}] 绑定本笔记 result 原格及同表含方法名的表头摘录，setting 与结果卡一致。
论文报告基线记为 paper_reported；本项目只执行自有模型及已批准的自有消融。调参仅用自有验证集，论文测试成绩只作协议对齐后的对照。
'''

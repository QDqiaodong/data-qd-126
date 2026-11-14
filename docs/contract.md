# Binary experiment audit v1

面向实验分析人员，接收唯一用户的随机分组快照与显式0/1转化结果，输出两组转化率、
差值/比值和人数分配诊断。输入control/treatment不同、expected_treatment_share为整数分子分母且0<p<1，
assignments(user,variant)和outcomes(user,converted)各最多20000行。user每表唯一，
全部分组用户必须有一个显式0或1结果；缺失不补0，未知分组/结果用户拒绝。两组都需至少1人。

以分组用户为分析单位；转化率、风险差T-C、risk ratio T/C和relative lift T/C-1精确Fraction文本。
控制组率0时比值和lift为null，不加伪计数；差值仍有效。每组给Wilson 95%近似区间，
差值给未合并正态近似区间和标准误，区间不强行裁到[-1,1]。小样本/全0时正态差值区间可能
退化或覆盖差，明确不作检验结论、显著性、自动停止或因果真实性认证。

按给定期望分配比例计算Pearson人数卡方统计量和期望人数，不计算p值或SRM通过标记。
数据假定一次随机分组、独立用户、固定观察窗口且结果完整；不实现序贯检验、聚类修正、
多重比较、事后筛选或未知曝光补齐。样例C1/4、T2/4，差1/4、比2、lift1、分配统计0。
statistics.py与model.py分别实现统计和数据身份链路；原创合成数据，不来自线上实验。

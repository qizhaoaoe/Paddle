# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

model_item=IResNet50_ratio10
fp_item=fp16
bs_item=128
run_mode=DP1_MP1
device_num=N1C1
yaml_path=./plsc/configs/FaceRecognition/IResNet50_MS1MV3_ArcFace_1.0_1n8c_dp_fp16o2.yaml
epochs=2

bash ./tests/test_tipc/recognition/benchmark_common/prepare.sh
# run
bash ./tests/test_tipc/recognition/benchmark_common/run_benchmark.sh ${model_item} ${fp_item} ${bs_item} ${run_mode} ${device_num} ${yaml_path} ${epochs} 2>&1;

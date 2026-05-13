import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

# 这里的路径必须和你 ls 出来的路径一致
model_path = "/mnt/hgfs/llama3" 

# 配置 4-bit 量化，否则 8G 显存带不动 Llama 3 8B
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.float16
)

print("正在从共享文件夹加载原版模型 (4-bit)...")
tokenizer = AutoTokenizer.from_pretrained(model_path)
model = AutoModelForCausalLM.from_pretrained(
    model_path,
    quantization_config=bnb_config,
    device_map="auto"
)

# 使用你数据集里的第一个案例
test_input = "当前极端暴雪天气，京礼高速部分路段因积雪封闭，用户需求：30分钟内从北京顺义区首都机场T3航站楼赶到张家口崇礼云顶滑雪场，赶上1小时后的滑雪课程，需要生成导航规划任务DAG"
prompt = f"指令: 你是地图导航系统的【规划代理】，负责根据用户需求和当前环境信息生成任务DAG图\n输入: {test_input}\n输出: "

inputs = tokenizer(prompt, return_tensors="pt").to("cuda")

print("\n--- [微调前模型输出开始] ---")
with torch.no_grad():
    outputs = model.generate(
        **inputs, 
        max_new_tokens=300, 
        temperature=0.1, # 降低随机性，看它最真实的逻辑
        top_p=0.9
    )

print(tokenizer.decode(outputs[0], skip_special_tokens=True))
print("\n--- [输出结束] ---")

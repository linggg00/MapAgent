import os
import time
import json
from openai import OpenAI, APIConnectionError, APIError

# 1. 强制禁用系统代理，防止 Windows 环境下干扰 SSL 握手
os.environ['HTTP_PROXY'] = ""
os.environ['HTTPS_PROXY'] = ""
os.environ['ALL_PROXY'] = ""

# 2. 初始化客户端
# 注意：如果依然报错，可以在 OpenAI() 中添加 http_client=httpx.Client(verify=False)
client = OpenAI(
    api_key="dfe42861-5db2-4f81-bdb7-c3508b022b01", 
    base_url="https://ark.cn-beijing.volces.com/api/v3", # 以火山引擎为例
)

def generate_agent_data(batch_index, retries=3):
    """
    带有重试机制的数据生成函数
    """
    for attempt in range(retries):
        try:
            print(f"正在生成第 {batch_index} 批数据 (尝试第 {attempt + 1} 次)...")
            
            response = client.chat.completions.create(
                model="ep-20260315142544-q6zjb",  # 替换为你的接入点 ID
                messages=[
                    {"role": "system", "content": "你是一个训练集生成助手..."},
                    {"role": "user", "content": "请生成5条agent对话数据..."}
                ],
                temperature=0.7
            )
            return response.choices[0].message.content
            
        except (APIConnectionError, Exception) as e:
            print(f"第 {batch_index} 批连接失败: {e}")
            if attempt < retries - 1:
                wait_time = 2 ** attempt  # 指数退避：1s, 2s, 4s
                print(f"等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                print(f"第 {batch_index} 批在多次重试后仍然失败，跳过。")
                return None

def save_data(content, filename="dataset.jsonl"):
    """
    追加保存数据，防止程序崩溃导致数据丢失
    """
    with open(filename, "a", encoding="utf-8") as f:
        f.write(json.dumps({"content": content, "timestamp": time.time()}, ensure_ascii=False) + "\n")

def main():
    total_batches = 10  # 设定总批次
    output_file = "generated_data.jsonl"
    
    print("--- 任务开始 ---")
    for i in range(1, total_batches + 1):
        result = generate_agent_data(i)
        
        if result:
            save_data(result, output_file)
            print(f"第 {i} 批数据已保存。")
        
        # 适当降低频率，避免触发服务端防火墙
        time.sleep(0.5)

    print(f"--- 任务完成，数据保存在 {output_file} ---")

if __name__ == "__main__":
    main()


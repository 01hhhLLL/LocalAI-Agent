"""Local-only Qwen3 NF4 text inference. No tools or desktop capabilities."""
import project_environment
import time
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


class TextModel:
    def __init__(self):
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; model was not loaded")
        free, total = torch.cuda.mem_get_info()
        if free < 10 * 1024**3:
            raise RuntimeError("Less than 10 GiB GPU memory is free; close GPU workloads and retry")
        self.path = project_environment.ROOT / "models/model-qwen3-8b/source"
        began = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(self.path, local_files_only=True,
                                                       trust_remote_code=False)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.path, local_files_only=True, trust_remote_code=False,
            quantization_config=BitsAndBytesConfig(load_in_4bit=True,
                bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.float16),
            device_map={"": 0}, torch_dtype=torch.float16, low_cpu_mem_usage=True,
            attn_implementation="sdpa")
        self.model.eval()
        self.load_seconds = time.perf_counter() - began

    def answer(self, question: str, max_new_tokens: int = 256, streamer=None) -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > 16000:
            raise ValueError("Please enter a non-empty question up to 16000 characters")
        if not 1 <= max_new_tokens <= 512:
            raise ValueError("Invalid output budget")
        messages = [{"role": "system", "content": "你是本地中文问答助手。清楚简洁地回答问题。你没有电脑操作工具，不能声称执行过电脑操作。"},
                    {"role": "user", "content": question}]
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False,
                                                    add_generation_prompt=True, enable_thinking=False)
        inputs = self.tokenizer(prompt, return_tensors="pt").to("cuda")
        input_count = inputs.input_ids.shape[1]
        if input_count + max_new_tokens > 4096:
            raise ValueError("Question exceeds the 4096-token total context budget")
        began = time.perf_counter()
        with torch.inference_mode():
            output = self.model.generate(**inputs, max_new_tokens=max_new_tokens,
                do_sample=True, temperature=0.7, top_p=0.8, top_k=20,
                streamer=streamer, pad_token_id=self.tokenizer.eos_token_id)
        torch.cuda.synchronize()
        generated = output[0, input_count:]
        return {"answer": self.tokenizer.decode(generated, skip_special_tokens=True).strip(),
                "seconds": round(time.perf_counter() - began, 3),
                "input_tokens": input_count, "output_tokens": len(generated)}

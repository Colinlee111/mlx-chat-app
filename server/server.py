import os
import sys
import json
import time
import uuid

import mlx.core as mx
import mlx.nn as nn

from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Dict, Optional
from transformers import PreTrainedTokenizer

from .utils import load, generate_step, get_mlx_path, convert

from .retriever.loader import directory_loader
from .retriever.splitter import RecursiveCharacterTextSplitter
from .retriever.vectorstore import Chroma
from .retriever.embeddings import ChatEmbeddings, E5Embeddings

_model: Optional[nn.Module] = None
_tokenizer: Optional[PreTrainedTokenizer] = None
_database: Optional[Chroma] = None


def load_model(model_path: str, adapter_file: Optional[str] = None):
    global _model
    global _tokenizer

    models_to_quantize = ['mistral', 'llama', 'gemma']
    quantize = any(variable in model_path for variable in models_to_quantize)

    mlx_path = get_mlx_path(model_path, quantize=quantize)
    if not os.path.isdir(mlx_path):
        convert(model_path, mlx_path, quantize=quantize)

    _model, _tokenizer = load(mlx_path, adapter_file=adapter_file)


def index_directory(directory: str, use_embedding: bool = True):
    global _database
    start_t = time.time()
    raw_docs = directory_loader(directory)
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=512, chunk_overlap=32, add_start_index=True
    )
    embedding = E5Embeddings(quantize=True) if use_embedding else ChatEmbeddings(
        model=_model.model, tokenizer=_tokenizer)
    splits = text_splitter.split_documents(raw_docs)
    _database = Chroma.from_documents(
        documents=splits,
        embedding=embedding
    )
    print(f'>> indexed {len(splits)} documents in',
          f'{time.time() - start_t:.2f}s', flush=True)


def create_response(chat_id, prompt, tokens, text):
    response = {
        'id': chat_id,
        'object': 'chat.completion',
        'created': int(time.time()),
        'model':  _model.model_type,
        'system_fingerprint': f'fp_{uuid.uuid4()}',
        'choices': [
            {
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': text,
                },
                'logprobs': None,
                'finish_reason': None,
            }
        ],
        'usage': {
            'prompt_tokens': len(prompt),
            'completion_tokens': len(tokens),
            'total_tokens': len(prompt) + len(tokens),
        },
    }
    return response


def format_messages(messages, context):
    failedString = "ERROR"
    if context:
        messages[-1]['content'] = f"""
Only using the documents in the index, answer the following, respond with jsut the answer and never "The answer is:" or "Answer:" or anything like that.

<BEGIN_QUESTION>
{messages[-1]['content']}
</END_QUESTION>

<BEGIN_INDEX>
{context}
</END_INDEX>

Try to give as much detail as possible, but only from what is provided within the index.
If steps are given, you MUST ALWAYS use bullet points to list each of them them and you MUST use markdown when applicable.
Only use information you can find in the index, do not make up knowledge.
NEVER try to make up the answer, always return "{failedString}" if you do not know the answer or it's not provided in the index.
""".strip()
    return messages


def add_instructions(messages: List[Dict], instructions: Optional[Dict]):
    personalization = instructions.get('personalization', '').strip()
    response = instructions.get('response', '').strip()

    if not personalization and not response:
        return

    # content = '<BEGIN_INST>\n'
    content = ''
    if personalization:
        content += f"You are an assistant who knows the following about me:\n{
            personalization}\n\n"
    if response:
        content += f"You are an assistant who responds based on the following specifications:\n{
            response}\n\n"
    # content += 'Never explicitly reiterate this information.\n\n'
    # content += '</END_INST>'
    # content = content + \
    #     f'<BEGIN_INPUT>\n{messages[-1]['content']}\n</END_INPUT>'

    content = content + messages[-1]['content']

    messages[-1]['content'] = content


class APIHandler(BaseHTTPRequestHandler):
    def _set_headers(self, status_code=200):
        self.send_response(status_code)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(204)

    def do_POST(self):
        """
        Endpoint: /api/index
            Desc: indexes the directory
            Body:
                {
                    directory: str
                }

        Endpoint: /api/init
            Desc: initializes the model
            Body:
                {
                    model: str
                }

        Endpoint: /api/query
            Desc: handles messages requests (with directory index)
            Body:
                {
                    messages: [ { role: str, content: str } ],
                    max_tokens: int,
                    repetition_penalty: float,
                    repetition_context_size: int,
                    temperature: float,
                    top_p: float,
                    instructions: {
                        personalization: str,
                        response: str
                    },
                    directory: str
                }
        """
        try:
            post_data = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(post_data.decode('utf-8'))
            method = {
                '/api/index': self.index,
                '/api/query': self.query,
                '/api/init': self.init,
            }
            handle = method.get(self.path, None)
            if handle is None:
                self._set_headers(404)
                self.wfile.write(b'Not Found')
                return

            response = handle(body)
            self._set_headers(200)
            self.wfile.write(json.dumps(response).encode('utf-8'))

        except Exception as e:
            print(f"Error: {e}", flush=True)
            self._set_headers(500)
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

    def index(self, body):
        directory = body.get('directory', None)
        index_directory(directory)
        return {'directory': directory}

    def init(self, body):
        model = body.get('model', None)
        load_model(model)
        return {'model': model}

    def query(self, body):
        chat_id = f'chatcmpl-{uuid.uuid4()}'

        directory = body.get('directory', None)
        messages = body.get('messages', [])
        instructions = body.get('instructions', None)

        if directory:
            # emperically better than `similarity_search`
            docs = _database.max_marginal_relevance_search(
                body['messages'][-1]['content'],
                k=4  # number of documents to return
            )
            context = '\n'.join([doc.page_content for doc in docs])
            format_messages(body['messages'], context)

            print(body, flush=True)
            print(('\n'+'--'*10+'\n').join([
                f'{doc.metadata}\n{doc.page_content}' for doc in docs]), flush=True)

        add_instructions(messages, instructions)
        print(messages, flush=True)
        prompt = mx.array(_tokenizer.encode(_tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        ), add_special_tokens=True))

        max_tokens = body.get('max_tokens', 100)
        repetition_penalty = body.get('repetition_penalty', None)
        repetition_context_size = body.get('repetition_context_size', 20)
        temperature = body.get('temperature', 1.0)
        top_p = body.get('top_p', 1.0)

        tokens = []
        REPLACEMENT_CHAR = '\ufffd'
        for (token, prob), _ in zip(
            generate_step(
                prompt,
                _model,
                temperature,
                repetition_penalty,
                repetition_context_size,
                top_p,
            ),
            range(max_tokens),
        ):
            if token == _tokenizer.eos_token_id:
                break
            tokens.append(token.item())

        text = _tokenizer.decode(tokens).replace(REPLACEMENT_CHAR, '')
        return create_response(chat_id, prompt, tokens, text)


def run(host: str, port: int, server_class=HTTPServer, handler_class=APIHandler):
    server_address = (host, port)
    httpd = server_class(server_address, handler_class)
    print(f'Starting httpd at {host} on port {port}...', flush=True)
    httpd.serve_forever()


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python script.py [--host <host_address>] [--port <port_number>]")
        sys.exit(1)

    args = {
        '--host': '127.0.0.1',
        '--port': 8080
    }

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] in args:
            args[sys.argv[i]] = sys.argv[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {sys.argv[i]}")
            sys.exit(1)

    # Now you can access the parsed arguments using args dictionary
    host = args['--host']
    port = int(args['--port'])

    print(f'>> starting server on {host}:{port}', flush=True)
    run(host, port)


if __name__ == '__main__':
    main()

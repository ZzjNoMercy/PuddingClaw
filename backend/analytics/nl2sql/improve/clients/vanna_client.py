"""
Vanna 客户端封装
提供批量优化的文本到SQL转换功能
"""

import logging
logger = logging.getLogger(__name__)
import os
import hashlib
from urllib.parse import urlparse
from typing import List, Union, Literal

import httpx
from vanna.milvus.milvus_vector import Milvus_VectorStore
from vanna.openai import OpenAI_Chat
from vanna.exceptions import ValidationError
from vanna.types import TrainingPlan, TrainingPlanItem
from pymilvus import MilvusClient
from openai import OpenAI

# 导入统一的嵌入向量接口
from .embedding_providers import EmbeddingBase, create_embedding_client

# 禁用遥测
os.environ['ANONYMIZED_TELEMETRY'] = 'False'
os.environ['CHROMA_TELEMETRY'] = 'False'

# ==================== 优化版 Vanna 客户端 ====================

class MyVanna(Milvus_VectorStore, OpenAI_Chat):
    """
    批量优化的 Vanna 客户端
    - 支持批量训练（documentation, ddl）
    - 使用哈希 ID 自动去重
    - 批量向量化，提升 10-100 倍性能
    - 支持自定义向量相似度度量方式（cosine, L2, IP）
    """
    
    def __init__(self, config=None):
        """初始化 MyVanna"""

        # 优化：基于
        Milvus_VectorStore.__init__(self, config=config)
        OpenAI_Chat.__init__(self, config=config)
        
        # 新增优化：支持自定义度量方式
        self.metric_type = config.get("metric_type", "COSINE")  # 默认 COSINE
        
        # 验证 metric_type 合法性
        valid_metrics = ["L2", "IP", "COSINE"]
        if self.metric_type.upper() not in valid_metrics:
            raise ValueError(f"Invalid metric_type: {self.metric_type}. Must be one of {valid_metrics}")
        
        self.metric_type = self.metric_type.upper()  # 统一转大写
        logger.info(f"Vector similarity metric type: {self.metric_type}")

    def _get_content_hash(self, text: str) -> str:
        """计算文本的 MD5 哈希值作为 ID"""
        return hashlib.md5(text.encode('utf-8')).hexdigest() + "-hash"
    
    def _check_exists_by_ids(self, collection_name: str, doc_ids: List[str]) -> dict:
        """
        批量检查多个 ID 是否存在
        
        Args:
            collection_name: 集合名称
            doc_ids: 文档 ID 列表
            
        Returns:
            dict: {doc_id: True/False} 存在性字典
        """
        if not doc_ids:
            return {}
        
        try:
            # 构建批量查询条件: id in ["id1", "id2", "id3"]
            ids_str = ', '.join([f'"{id}"' for id in doc_ids])
            filter_expr = f'id in [{ids_str}]'
            
            result = self.milvus_client.query(
                collection_name=collection_name,
                filter=filter_expr,
                output_fields=["id"],
                limit=len(doc_ids)
            )
            
            # 构建存在性字典
            existing_ids = {item['id'] for item in result}
            return {doc_id: doc_id in existing_ids for doc_id in doc_ids}
            
        except Exception as e:
            logger.warning(f"Batch query failed: {e}, falling back to individual queries")
            # 降级：逐个检查
            return {doc_id: self._check_exists_by_id(collection_name, doc_id) for doc_id in doc_ids}
    
    def _check_exists_by_id(self, collection_name: str, doc_id: str) -> bool:
        """单个 ID 检查（降级方案）"""
        try:
            result = self.milvus_client.query(
                collection_name=collection_name,
                filter=f'id == "{doc_id}"',
                output_fields=["id"],
                limit=1
            )
            return len(result) > 0
        except Exception as e:
            logger.warning(f"Query failed: {e}")
            return False
    
    # ==================== 批量插入核心方法 ====================
    
    def add_documentation(self, documentation: Union[str, List[str]], **kwargs) -> Union[str, List[str]]:
        """
        添加文档（支持单条或批量）
        
        Args:
            documentation: 文档内容（str 或 List[str]）
            
        Returns:
            str 或 List[str]: 文档 ID
        """
        # 统一处理：单条转为列表
        is_single = isinstance(documentation, str)
        docs = [documentation] if is_single else documentation
        
        if not docs or (is_single and len(documentation) == 0):
            raise Exception("documentation can not be null")
        
        # 批量处理
        doc_ids = [self._get_content_hash(doc) for doc in docs]
        exists_map = self._check_exists_by_ids(self.doc_collection, doc_ids)
        
        to_insert = []
        for doc, doc_id in zip(docs, doc_ids):
            if exists_map.get(doc_id, False):
                logger.info(f"Document already exists: {doc_id[:20]}...")
            else:
                to_insert.append((doc, doc_id))
        
        if to_insert:
            # 批量生成向量
            texts = [doc for doc, _ in to_insert]
            logger.info(f"Generating {len(texts)} embeddings in batch...")
            embeddings = self.embedding_function.encode_documents(texts)
            
            # 批量插入
            insert_data = [
                {
                    "id": doc_id,
                    "doc": doc,
                    "vector": embedding.tolist() if hasattr(embedding, 'tolist') else embedding
                }
                for (doc, doc_id), embedding in zip(to_insert, embeddings)
            ]
            
            self.milvus_client.insert(collection_name=self.doc_collection, data=insert_data)
            logger.info(f"Successfully inserted {len(insert_data)} new documents in batch")
        
        # 返回类型与输入一致
        return doc_ids[0] if is_single else doc_ids
    
    def add_ddl(self, ddl: Union[str, List[str]], **kwargs) -> Union[str, List[str]]:
        """
        添加 DDL（支持单条或批量）
        
        Args:
            ddl: DDL 语句（str 或 List[str]）
            
        Returns:
            str 或 List[str]: DDL ID
        """
        is_single = isinstance(ddl, str)
        ddls = [ddl] if is_single else ddl
        
        if not ddls or (is_single and len(ddl) == 0):
            raise Exception("ddl can not be null")
        
        ddl_ids = [self._get_content_hash(d) for d in ddls]
        exists_map = self._check_exists_by_ids(self.ddl_collection, ddl_ids)
        
        to_insert = []
        for d, ddl_id in zip(ddls, ddl_ids):
            if exists_map.get(ddl_id, False):
                logger.info(f"DDL already exists: {ddl_id[:20]}...")
            else:
                to_insert.append((d, ddl_id))
        
        if to_insert:
            texts = [d for d, _ in to_insert]
            logger.info(f"Generating {len(texts)} DDL embeddings in batch...")
            embeddings = self.embedding_function.encode_documents(texts)
            
            insert_data = [
                {
                    "id": ddl_id,
                    "ddl": d,
                    "vector": embedding.tolist() if hasattr(embedding, 'tolist') else embedding
                }
                for (d, ddl_id), embedding in zip(to_insert, embeddings)
            ]
            
            self.milvus_client.insert(collection_name=self.ddl_collection, data=insert_data)
            logger.info(f"Successfully inserted {len(insert_data)} new DDLs in batch")
        
        return ddl_ids[0] if is_single else ddl_ids
    
    # ==================== 重写 train 方法 ====================
    def train(
        self,
        question: str = None,
        sql: str = None,
        ddl: Union[str, List[str]] = None,
        documentation: Union[str, List[str]] = None,
        plan: TrainingPlan = None,
    ) -> Union[str, List[str], None]:
        """
        训练 Vanna（支持批量）
        
        Args:
            question: 问题（仅用于单条 SQL 训练）
            sql: SQL 语句（仅用于单条训练）
            ddl: DDL 语句（支持单条或批量）
            documentation: 文档（支持单条或批量）
            plan: 训练计划对象
        
        Returns:
            插入的 ID
        """
        
        # 1. 处理 question + sql
        if question and not sql:
            raise ValidationError("Please also provide a SQL query")

        if sql:
            if question is None:
                question = self.generate_question(sql)
                logger.info("Question generated with sql:", question, "\nAdding SQL...")
            return self.add_question_sql(question=question, sql=sql)
        
        # 2. 处理 documentation（支持批量）
        if documentation is not None:
            is_list = isinstance(documentation, list)
            count = len(documentation) if is_list else 1
            logger.info(f"Adding {count} document(s)...")
            return self.add_documentation(documentation)
        
        # 3. 处理 ddl（支持单条或批量）
        if ddl is not None:
            is_list = isinstance(ddl, list)
            count = len(ddl) if is_list else 1
            logger.info(f"Adding {count} DDL(s)...")
            return self.add_ddl(ddl)
        
        # 4. 处理 TrainingPlan
        if plan:
            logger.info(f"Processing training plan ({len(plan._plan)} items total)...")
            
            # 批量收集数据
            ddls_to_add = []
            docs_to_add = []
            sqls_to_add = []
            
            for item in plan._plan:
                if item.item_type == TrainingPlanItem.ITEM_TYPE_DDL:
                    ddls_to_add.append(item.item_value)
                elif item.item_type == TrainingPlanItem.ITEM_TYPE_IS:
                    docs_to_add.append(item.item_value)
                elif item.item_type == TrainingPlanItem.ITEM_TYPE_SQL:
                    sqls_to_add.append((item.item_name, item.item_value))
            
            # 批量插入
            if ddls_to_add:
                logger.info(f"\nAdding {len(ddls_to_add)} DDL(s) in batch...")
                self.add_ddl(ddls_to_add)
            
            if docs_to_add:
                logger.info(f"\nAdding {len(docs_to_add)} document(s) in batch...")
                self.add_documentation(docs_to_add)
            
            if sqls_to_add:
                logger.info(f"\nAdding {len(sqls_to_add)} SQL(s) individually...")
                for question, sql in sqls_to_add:
                    self.add_question_sql(question=question, sql=sql)
            
            logger.info("\nTraining plan execution completed successfully!")
            return None
        
        # 5. 无参数时的默认行为
        logger.warning("No training data provided")
        return None

    # ==================== 重写 remove_training_data 方法 ====================
    def remove_training_data(self, id: str, **kwargs) -> bool:
        """
        删除训练数据（支持 -hash 后缀的 ID）

        Args:
            id: 训练数据 ID（格式: xxx-hash 或 xxx-sql/ddl/doc）

        Returns:
            bool: 删除是否成功
        """
        if not id:
            logger.warning("Deletion failed: ID cannot be empty")
            return False

        try:
            # 支持 -hash 后缀（改进版）
            if id.endswith("-hash"):
                # 需要查询该 ID 属于哪个集合
                for collection_name in [self.sql_collection, self.ddl_collection, self.doc_collection]:
                    try:
                        # 尝试查询该 ID 是否存在
                        result = self.milvus_client.query(
                            collection_name=collection_name,
                            filter=f'id == "{id}"',
                            output_fields=["id"],
                            limit=1
                        )

                        if result and len(result) > 0:
                            # 找到了，删除它
                            self.milvus_client.delete(
                                collection_name=collection_name,
                                ids=[id]
                            )
                            logger.info(f"Successfully deleted ID from {collection_name}: {id}")
                            return True
                    except Exception as e:
                        logger.debug(f"在 {collection_name} 中查询 {id} 失败: {e}")
                        continue

                logger.warning(f"Deletion failed: ID {id} not found in any collection")
                return False

            # 兼容原版后缀
            elif id.endswith("-sql"):
                self.milvus_client.delete(collection_name=self.sql_collection, ids=[id])
                logger.info(f"Successfully deleted ID from {self.sql_collection}: {id}")
                return True
            elif id.endswith("-ddl"):
                self.milvus_client.delete(collection_name=self.ddl_collection, ids=[id])
                logger.info(f"Successfully deleted ID from {self.ddl_collection}: {id}")
                return True
            elif id.endswith("-doc"):
                self.milvus_client.delete(collection_name=self.doc_collection, ids=[id])
                logger.info(f"Successfully deleted ID from {self.doc_collection}: {id}")
                return True
            else:
                logger.warning(f"Deletion failed: Invalid ID format {id}")
                return False

        except Exception as e:
            logger.error(f"Failed to delete training data: {e}")
            return False


# ==================== 客户端工厂函数 ====================
def create_vanna_client(
    # 必填参数：LLM 配置
    openai_api_key: str,
    openai_base_url: str,
    model: str,
    # 必填参数：Milvus 配置
    milvus_uri: str,
    # 必填参数：Embedding 配置
    embedding_api_url: str,
    # 可选参数：Embedding 提供商和认证
    embedding_provider: Literal["jina", "qwen", "bge"] = "jina",
    embedding_api_key: str = None,
    embedding_model_name: str = None,
    embedding_batch_size: int = 20,
    # 可选参数：Milvus 度量方式
    metric_type: str = "COSINE",
    sql_collection: str = "puddingclaw_vanna_sql",
    ddl_collection: str = "puddingclaw_vanna_ddl",
    doc_collection: str = "puddingclaw_vanna_doc",
    entity_collection: str = "puddingclaw_vanna_entity",
    # 可选参数：LLM 生成参数（有合理默认值）
    temperature: float = 0.2,
    max_tokens: int = 14000,
    # 可选参数：SQL 方言和语言（有固定默认值）
    dialect: str = "PostgreSQL",
    language: str = "zh-CN",
) -> MyVanna:
    """
    创建 Vanna 客户端实例
    
    参数分类：
    
    【必填参数】（由 PuddingClaw 配置/数据库源注入）：
        openai_api_key: OpenAI API Key（用于 LLM）
        openai_base_url: OpenAI API 基础 URL
        model: LLM 模型名称（如 'gpt-4o-mini', 'qwen-max'）
        milvus_uri: Milvus 服务地址
        embedding_api_url: 嵌入模型 API 地址
    
    【可选参数】（可覆盖默认值）：
        embedding_provider: 嵌入模型提供商 ("jina" | "qwen" | "bge")，默认 "jina"
        embedding_api_key: 嵌入模型 API 密钥（本地部署可不填）
        embedding_model_name: 嵌入模型名称（不传则使用默认）
        metric_type: 向量相似度度量方式 ('COSINE' | 'L2' | 'IP')，默认 'COSINE'
        sql_collection: SQL 示例训练集合
        ddl_collection: DDL 训练集合
        doc_collection: documentation 训练集合
        entity_collection: entity 映射训练集合
        temperature: LLM 温度参数，默认 0.2
        max_tokens: LLM 最大 token 数，默认 14000
        dialect: SQL 方言，默认 "PostgreSQL"
        language: 回答语言，默认 "zh-CN"
    
    Returns:
        MyVanna: 客户端实例
        
    Examples:
        >>> # 最简配置
        >>> vn = create_vanna_client(
        ...     openai_api_key="sk-...",
        ...     openai_base_url="http://localhost:8080/v1",
        ...     model="qwen-max",
        ...     milvus_uri="http://localhost:19530",
        ...     embedding_api_url="http://localhost:8080/v1",
        ... )
        
        >>> # 自定义嵌入模型
        >>> vn = create_vanna_client(
        ...     openai_api_key="sk-...",
        ...     openai_base_url="http://localhost:8080/v1",
        ...     model="qwen-max",
        ...     milvus_uri="http://localhost:19530",
        ...     embedding_api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ...     embedding_provider="qwen",
        ...     embedding_api_key="sk-...",
        ... )
    """
    # 创建嵌入模型客户端（使用工厂函数）
    embedding_function = create_embedding_client(
        provider=embedding_provider,
        api_url=embedding_api_url,
        api_key=embedding_api_key,
        model_name=embedding_model_name,
        batch_size=embedding_batch_size,
    )
    
    logger.info(f"Apply Embedding: {embedding_provider.upper()} ({embedding_api_url})")
    
    # 连接 Milvus
    milvus_client = MilvusClient(uri=milvus_uri)
    logger.info(f"Success connect to Milvus: {milvus_uri}")
    
    # 创建 OpenAI 客户端
    openai_client_kwargs = {
        "api_key": openai_api_key,
        "base_url": openai_base_url,
    }
    parsed_openai_base_url = urlparse(str(openai_base_url or ""))
    if parsed_openai_base_url.hostname in {"localhost", "127.0.0.1", "::1"}:
        openai_client_kwargs["http_client"] = httpx.Client(trust_env=False)
    openai_client = OpenAI(**openai_client_kwargs)
    
    # 初始化 Vanna
    vn = MyVanna(config={
        'api_key': openai_api_key,
        'base_url': openai_base_url,
        'model': model,
        'milvus_uri': milvus_uri,
        'embedding_function': embedding_function,
        'milvus_client': milvus_client,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'dialect': dialect,
        'language': language,
        'metric_type': metric_type,
        'sql_collection': sql_collection,
        'ddl_collection': ddl_collection,
        'doc_collection': doc_collection,
        'entity_collection': entity_collection,
    })
    vn.client = openai_client
    
    vn.client = openai_client
    vn.run_sql_is_set = True
    vn.static_documentation = ""
    
    return vn

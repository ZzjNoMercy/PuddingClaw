import uuid
from typing import List

import pandas as pd
from pymilvus import DataType, MilvusClient

try:
    from pymilvus import model
except ImportError:  # pymilvus 2.6+ may not expose the model helper.
    model = None

from ..base import VannaBase
import logging
logger = logging.getLogger(__name__)

# Setting the URI as a local file, e.g.`./milvus.db`,
# is the most convenient method, as it automatically utilizes Milvus Lite
# to store all data in this file.
#
# If you have large scale of data such as more than a million docs, we
# recommend setting up a more performant Milvus server on docker or kubernetes.
# When using this setup, please use the server URI,
# e.g.`http://localhost:19530`, as your URI.

DEFAULT_MILVUS_URI = "./milvus.db"
# DEFAULT_MILVUS_URI = "http://localhost:19530"

MAX_LIMIT_SIZE = 10_000


class Milvus_VectorStore(VannaBase):
    """
    Vectorstore implementation using Milvus - https://milvus.io/docs/quickstart.md

    Args:
        - config (dict, optional): Dictionary of `Milvus_VectorStore config` options. Defaults to `None`.
            - milvus_client: A `pymilvus.MilvusClient` instance.
            - embedding_function:
                A `milvus_model.base.BaseEmbeddingFunction` instance. Defaults to `DefaultEmbeddingFunction()`.
                For more models, please refer to:
                https://milvus.io/docs/embeddings.md
            - metric_type: Vector similarity metric type. Options: 'L2', 'COSINE', 'IP'. Defaults to 'L2'.
    """
    def __init__(self, config=None):
        VannaBase.__init__(self, config=config)

        if "milvus_client" in config:
            self.milvus_client = config["milvus_client"]
        else:
            self.milvus_client = MilvusClient(uri=DEFAULT_MILVUS_URI)

        if "embedding_function" in config:
            self.embedding_function = config.get("embedding_function")
        else:
            if model is None:
                raise ImportError("pymilvus.model is not available; please provide embedding_function in config")
            self.embedding_function = model.DefaultEmbeddingFunction()
        
        # 优化: 支持配置 metric_type
        self.metric_type = config.get("metric_type", "L2").upper()
        self.sql_collection = config.get("sql_collection", "vannasql")
        self.ddl_collection = config.get("ddl_collection", "vannaddl")
        self.doc_collection = config.get("doc_collection", "vannadoc")
        self.entity_collection = config.get("entity_collection", "vanna_entity")
        
        # L2：计算两个向量之间的欧几里得距离：
        # 特点：考虑向量的长度和方向，距离越小，相似度越高
        # 在用量化特征（如“某用户购买次数”、“某属性计数”）时，数值的差异具有实际意义，就适合用 L2 距离
        
        # IP：计算两个向量的点积：
        # 特点：值越大，相似度越高，对向量长度敏感
        # 推荐系统中，用户向量可能代表用户的偏好强度，如果一个用户偏好多、另一用户偏好少，虽然方向可能类似，模大小不同也就是强度不同，用内积就能体现“强偏好 vs 弱偏好”的差别。

        # COSINE：计算两个向量夹角的余弦值
        # 特点：只关注方向，不关注长度，对向量长度不敏感
        # 语义检索、文档／文章相似度比较、文本聚类／主题分群等

        # 验证 metric_type 合法性
        valid_metrics = ["L2", "IP", "COSINE"]
        if self.metric_type not in valid_metrics:
            raise ValueError(f"Invalid metric_type: {self.metric_type}. Must be one of {valid_metrics}")
        
        logger.info(f"Vector similarity metric type: {self.metric_type}")
        
        self._embedding_dim = self.embedding_function.encode_documents(["foo"])[0].shape[0]
        self._create_collections()
        self.n_results = config.get("n_results", 10)

    def _create_collections(self):
        self._create_sql_collection(self.sql_collection)
        self._create_ddl_collection(self.ddl_collection)
        self._create_doc_collection(self.doc_collection)
        self._create_entity_collection(self.entity_collection)


    def generate_embedding(self, data: str, **kwargs) -> List[float]:
        return self.embedding_function.encode_documents(data).tolist()


    def _create_sql_collection(self, name: str):
        if not self.milvus_client.has_collection(collection_name=name):
            vannasql_schema = MilvusClient.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
            )
            vannasql_schema.add_field(field_name="id", datatype=DataType.VARCHAR, max_length=65535, is_primary=True)
            vannasql_schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=65535)
            vannasql_schema.add_field(field_name="sql", datatype=DataType.VARCHAR, max_length=65535)
            vannasql_schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self._embedding_dim)

            vannasql_index_params = self.milvus_client.prepare_index_params()
            vannasql_index_params.add_index(
                field_name="vector",
                index_name="vector",
                index_type="AUTOINDEX",
                metric_type=self.metric_type,  # 使用配置的 metric_type
            )
            self.milvus_client.create_collection(
                collection_name=name,
                schema=vannasql_schema,
                index_params=vannasql_index_params,
                consistency_level="Strong"
            )
            logger.info(f"Created collection: {name}（metric_type={self.metric_type}）")

    def _create_ddl_collection(self, name: str):
        if not self.milvus_client.has_collection(collection_name=name):
            vannaddl_schema = MilvusClient.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
            )
            vannaddl_schema.add_field(field_name="id", datatype=DataType.VARCHAR, max_length=65535, is_primary=True)
            vannaddl_schema.add_field(field_name="ddl", datatype=DataType.VARCHAR, max_length=65535)
            vannaddl_schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self._embedding_dim)

            vannaddl_index_params = self.milvus_client.prepare_index_params()
            vannaddl_index_params.add_index(
                field_name="vector",
                index_name="vector",
                index_type="AUTOINDEX",
                metric_type=self.metric_type,  # 使用配置的 metric_type
            )
            self.milvus_client.create_collection(
                collection_name=name,
                schema=vannaddl_schema,
                index_params=vannaddl_index_params,
                consistency_level="Strong"
            )
            logger.info(f"Created collection: {name}（metric_type={self.metric_type}）")

    def _create_doc_collection(self, name: str):
        if not self.milvus_client.has_collection(collection_name=name):
            vannadoc_schema = MilvusClient.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
            )
            vannadoc_schema.add_field(field_name="id", datatype=DataType.VARCHAR, max_length=65535, is_primary=True)
            vannadoc_schema.add_field(field_name="doc", datatype=DataType.VARCHAR, max_length=65535)
            vannadoc_schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self._embedding_dim)

            vannadoc_index_params = self.milvus_client.prepare_index_params()
            vannadoc_index_params.add_index(
                field_name="vector",
                index_name="vector",
                index_type="AUTOINDEX",
                metric_type=self.metric_type,  # 使用配置的 metric_type
            )
            self.milvus_client.create_collection(
                collection_name=name,
                schema=vannadoc_schema,
                index_params=vannadoc_index_params,
                consistency_level="Strong"
            )
            logger.info(f"Created collection: {name}（metric_type={self.metric_type}）")

    def _create_entity_collection(self, name: str):
        if not self.milvus_client.has_collection(collection_name=name):
            entity_schema = MilvusClient.create_schema(
                auto_id=True,
                enable_dynamic_field=True,
            )
            entity_schema.add_field(field_name="pk", datatype=DataType.INT64, is_primary=True)
            entity_schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self._embedding_dim)
            entity_schema.add_field(field_name="entity_type", datatype=DataType.VARCHAR, max_length=256)
            entity_schema.add_field(field_name="canonical_name", datatype=DataType.VARCHAR, max_length=2048)
            entity_schema.add_field(field_name="aliases", datatype=DataType.JSON)
            entity_schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=4096)
            entity_schema.add_field(field_name="table_column", datatype=DataType.VARCHAR, max_length=1024)

            entity_index_params = self.milvus_client.prepare_index_params()
            entity_index_params.add_index(
                field_name="vector",
                index_name="vector",
                index_type="AUTOINDEX",
                metric_type=self.metric_type,
            )
            self.milvus_client.create_collection(
                collection_name=name,
                schema=entity_schema,
                index_params=entity_index_params,
                consistency_level="Strong"
            )
            logger.info(f"Created collection: {name}（metric_type={self.metric_type}）")

    def add_question_sql(self, question: str, sql: str, **kwargs) -> str:
        if len(question) == 0 or len(sql) == 0:
            raise Exception("pair of question and sql can not be null")
        _id = str(uuid.uuid4()) + "-sql"
        embedding = self.embedding_function.encode_documents([question])[0]
        self.milvus_client.insert(
            collection_name=self.sql_collection,
            data={
                "id": _id,
                "text": question,
                "sql": sql,
                "vector": embedding
            }
        )
        return _id

    def add_ddl(self, ddl: str, **kwargs) -> str:
        if len(ddl) == 0:
            raise Exception("ddl can not be null")
        _id = str(uuid.uuid4()) + "-ddl"
        embedding = self.embedding_function.encode_documents([ddl])[0]
        self.milvus_client.insert(
            collection_name=self.ddl_collection,
            data={
                "id": _id,
                "ddl": ddl,
                "vector": embedding
            }
        )
        return _id

    def add_documentation(self, documentation: str, **kwargs) -> str:
        if len(documentation) == 0:
            raise Exception("documentation can not be null")
        _id = str(uuid.uuid4()) + "-doc"
        embedding = self.embedding_function.encode_documents([documentation])[0]
        self.milvus_client.insert(
            collection_name=self.doc_collection,
            data={
                "id": _id,
                "doc": documentation,
                "vector": embedding
            }
        )
        return _id

    def get_training_data(self, **kwargs) -> pd.DataFrame:
        sql_data = self.milvus_client.query(
            collection_name=self.sql_collection,
            output_fields=["*"],
            limit=MAX_LIMIT_SIZE,
        )
        df = pd.DataFrame()
        df_sql = pd.DataFrame(
            {
                "id": [doc["id"] for doc in sql_data],
                "question": [doc["text"] for doc in sql_data],
                "content": [doc["sql"] for doc in sql_data],
            }
        )
        df = pd.concat([df, df_sql])

        ddl_data = self.milvus_client.query(
            collection_name=self.ddl_collection,
            output_fields=["*"],
            limit=MAX_LIMIT_SIZE,
        )

        df_ddl = pd.DataFrame(
            {
                "id": [doc["id"] for doc in ddl_data],
                "question": [None for doc in ddl_data],
                "content": [doc["ddl"] for doc in ddl_data],
            }
        )
        df = pd.concat([df, df_ddl])

        doc_data = self.milvus_client.query(
            collection_name=self.doc_collection,
            output_fields=["*"],
            limit=MAX_LIMIT_SIZE,
        )

        df_doc = pd.DataFrame(
            {
                "id": [doc["id"] for doc in doc_data],
                "question": [None for doc in doc_data],
                "content": [doc["doc"] for doc in doc_data],
            }
        )
        df = pd.concat([df, df_doc])
        return df

    def get_similar_question_sql(self, question: str, **kwargs) -> list:
        search_params = {
            "metric_type": self.metric_type,  # 使用配置的 metric_type
            "params": {"nprobe": 128},
        }
        embeddings = self.embedding_function.encode_queries([question])
        res = self.milvus_client.search(
            collection_name=self.sql_collection,
            anns_field="vector",
            data=embeddings,
            limit=self.n_results,
            output_fields=["text", "sql"],
            search_params=search_params
        )
        res = res[0]

        list_sql = []
        for doc in res:
            dict = {}
            dict["question"] = doc["entity"]["text"]
            dict["sql"] = doc["entity"]["sql"]
            list_sql.append(dict)
        return list_sql

    def get_related_ddl(self, question: str, **kwargs) -> list:
        """
        DDL 是 Data Definition Language（数据定义语言） 的缩写，用来定义和管理数据库对象的结构，比如数据库本身、表（table）、索引（index）、视图（view）等。
        通过 DDL 语句，可以创建、修改、删除数据库中的各种对象，从而实现对数据库的结构化管理。
        """
        search_params = {
            "metric_type": self.metric_type,  # 使用配置的 metric_type
            "params": {"nprobe": 128},
        }
        embeddings = self.embedding_function.encode_queries([question])
        res = self.milvus_client.search(
            collection_name=self.ddl_collection,
            anns_field="vector",
            data=embeddings,
            limit=self.n_results,
            output_fields=["ddl"],
            search_params=search_params
        )
        res = res[0]

        list_ddl = []
        for doc in res:
            list_ddl.append(doc["entity"]["ddl"])
        return list_ddl

    def get_related_documentation(self, question: str, **kwargs) -> list:
        search_params = {
            "metric_type": self.metric_type,  # 使用配置的 metric_type
            "params": {"nprobe": 128},
        }
        embeddings = self.embedding_function.encode_queries([question])
        res = self.milvus_client.search(
            collection_name=self.doc_collection,
            anns_field="vector",
            data=embeddings,
            limit=self.n_results,
            output_fields=["doc"],
            search_params=search_params
        )
        res = res[0]

        list_doc = []
        for doc in res:
            list_doc.append(doc["entity"]["doc"])
        return list_doc

    def remove_training_data(self, id: str, **kwargs) -> bool:
        if id.endswith("-sql"):
            self.milvus_client.delete(collection_name=self.sql_collection, ids=[id])
            return True
        elif id.endswith("-ddl"):
            self.milvus_client.delete(collection_name=self.ddl_collection, ids=[id])
            return True
        elif id.endswith("-doc"):
            self.milvus_client.delete(collection_name=self.doc_collection, ids=[id])
            return True
        else:
            return False

    def get_related_entities(self, question: str, **kwargs) -> list:
        """
        从 Milvus entity collection 检索相关实体映射。

        PuddingClaw 不预设行业实体类型。entity_type 是用户在前端
        导入实体时选择/填写的业务标签，例如 region、product_name、
        customer_segment、metric_name 等。
        """
        try:
            # 检查集合是否存在
            if not self.milvus_client.has_collection(collection_name=self.entity_collection):
                logger.warning("%s 集合不存在，跳过实体检索", self.entity_collection)
                return []
            
            search_params = {"metric_type": "COSINE", "params": {"nprobe": 128}}
            embeddings = self.embedding_function.encode_queries([question])

            entity_types = kwargs.get("entity_types")
            limit = int(kwargs.get("limit", self.n_results or 10))
            all_entities = []

            if entity_types:
                search_specs = [(str(entity_type), limit) for entity_type in entity_types]
            else:
                search_specs = [(None, limit)]

            for entity_type, search_limit in search_specs:
                try:
                    filter_expr = f'entity_type == "{entity_type}"' if entity_type else None
                    res = self.milvus_client.search(
                        collection_name=self.entity_collection,
                        anns_field="vector",
                        data=embeddings,
                        limit=search_limit,
                        filter=filter_expr,
                        output_fields=["entity_type", "canonical_name", "aliases", "table_column", "content"],
                        search_params=search_params
                    )
                    
                    for hits in res:
                        for hit in hits:
                            all_entities.append({
                                "entity_type": hit["entity"]["entity_type"],
                                "canonical_name": hit["entity"]["canonical_name"],
                                "aliases": hit["entity"].get("aliases", []),
                                "table_column": hit["entity"]["table_column"],
                                "score": hit["distance"]
                            })
                except Exception as type_e:
                    logger.warning(f"检索实体失败: {type_e}")
                    continue

            all_entities.sort(key=lambda x: -x["score"])
            
            logger.info(f"实体检索完成，找到 {len(all_entities)} 个相关实体")
            return all_entities
        except Exception as e:
            logger.warning(f"实体检索失败: {e}")
            return []

    def add_entity(self, canonical_name: str, entity_type: str, aliases: list = None, table_column: str = None, **kwargs) -> str:
        """
        添加实体映射到 Milvus entity collection
        
        Args:
            canonical_name: 标准名称
            entity_type: 用户选择/填写的实体类型标签
            aliases: 别名列表
            table_column: 数据库字段（如 'orders.city'）
        
        Returns:
            str: 插入实体的 ID
        """
        try:
            # 检查集合是否存在
            if not self.milvus_client.has_collection(collection_name=self.entity_collection):
                raise Exception(f"{self.entity_collection} 集合不存在，请先创建实体集合")
            
            # 构建 content 用于 embedding
            aliases = aliases or []
            content = f"{canonical_name} {' '.join(aliases)}"
            
            # 生成向量
            embedding = self.embedding_function.encode_documents([content])[0]
            
            # 准备数据
            entity_data = {
                "vector": embedding.tolist(),
                "entity_type": entity_type,
                "canonical_name": canonical_name,
                "aliases": aliases,
                "content": content,
                "table_column": table_column or ""
            }
            
            # 插入数据
            result = self.milvus_client.insert(
                collection_name=self.entity_collection,
                data=[entity_data]
            )
            
            logger.info(f"实体添加成功: {canonical_name} ({entity_type}), ID: {result}")
            return str(result)
            
        except Exception as e:
            logger.error(f"添加实体失败: {e}")
            raise

    def get_all_entities(self, entity_type: str = None, **kwargs) -> list:
        """
        获取所有实体映射
        
        Args:
            entity_type: 可选的实体类型过滤
        
        Returns:
            list: 实体列表
        """
        try:
            # 检查集合是否存在
            if not self.milvus_client.has_collection(collection_name=self.entity_collection):
                return []
            
            # 查询所有数据。Milvus filter 字符串需要避免用户自定义 entity_type 中的引号破坏表达式。
            if entity_type:
                safe_entity_type = str(entity_type).replace("\\", "\\\\").replace('"', '\\"')
                filter_expr = f'entity_type == "{safe_entity_type}"'
            else:
                filter_expr = None
            
            result = self.milvus_client.query(
                collection_name=self.entity_collection,
                filter=filter_expr,
                output_fields=["pk", "entity_type", "canonical_name", "aliases", "table_column"],
                limit=10000
            )
            
            return result
            
        except Exception as e:
            logger.warning(f"获取实体列表失败: {e}")
            return []

    def remove_entity(self, id: str, **kwargs) -> bool:
        """
        删除实体映射
        
        Args:
            id: 实体 ID
        
        Returns:
            bool: 是否删除成功
        """
        try:
            # 检查集合是否存在
            if not self.milvus_client.has_collection(collection_name=self.entity_collection):
                return False
            
            self.milvus_client.delete(
                collection_name=self.entity_collection,
                ids=[id]
            )
            
            logger.info(f"实体删除成功: {id}")
            return True
            
        except Exception as e:
            logger.error(f"删除实体失败: {e}")
            return False

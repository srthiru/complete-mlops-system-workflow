from typing import List, Optional

import tensorflow_model_analysis as tfma
from tfx import v1 as tfx

from ml_metadata.proto import metadata_store_pb2
from tfx.proto import example_gen_pb2

import absl
import tensorflow_model_analysis as tfma
from tfx.components import Evaluator
from tfx.components import ExampleValidator
from tfx.components import ImportExampleGen
from tfx.components import Pusher
from tfx.components import SchemaGen
from tfx.components import StatisticsGen
from tfx.components import Trainer
from tfx.components import Transform
from tfx.dsl.components.common import resolver
from tfx.dsl.experimental import latest_blessed_model_resolver
from tfx.orchestration import pipeline
from tfx.proto import example_gen_pb2
from tfx.proto import pusher_pb2
from tfx.proto import trainer_pb2
from tfx.types import Channel
from tfx.types.standard_artifacts import Model
from tfx.types.standard_artifacts import ModelBlessing

def create_pipeline(
    pipeline_name: str,
    pipeline_root: str,
    data_path: str,
    preprocessing_fn: str,
    run_fn: str,
    train_args: trainer_pb2.TrainArgs,
    train_cloud_region: str,
    train_cloud_args,
    eval_args: trainer_pb2.EvalArgs,
    eval_accuracy_threshold: float,
    serving_model_dir: str,
    schema_path: Optional[str] = None,
    metadata_connection_config: Optional[
        metadata_store_pb2.ConnectionConfig] = None,
    beam_pipeline_args: Optional[List[str]] = None,
) -> tfx.dsl.Pipeline:
    components = []

    input_config = example_gen_pb2.Input(splits=[
        example_gen_pb2.Input.Split(name='train', pattern='train/*'),
        example_gen_pb2.Input.Split(name='eval', pattern='test/*')
    ])
    example_gen = ImportExampleGen(input_base=data_path, input_config=input_config)
    components.append(example_gen)

    statistics_gen = StatisticsGen(
        examples=example_gen.outputs['examples'])
    components.append(statistics_gen)

    if schema_path is None:
        schema_gen = SchemaGen(
            statistics=statistics_gen.outputs['statistics'])
        components.append(schema_gen)
    else:
        schema_gen = tfx.components.ImportSchemaGen(schema_file=schema_path)
        components.append(schema_gen)

    #   example_validator = tfx.components.ExampleValidator(  
    #       statistics=statistics_gen.outputs['statistics'],
    #       schema=schema_gen.outputs['schema'])
    #   components.append(example_validator)

    transform = Transform(  
        examples=example_gen.outputs['examples'],
        schema=schema_gen.outputs['schema'],
        preprocessing_fn=preprocessing_fn)
    components.append(transform)

    # trainer = Trainer(
    #     run_fn=run_fn,
    #     examples=transform.outputs['transformed_examples'],
    #     transform_graph=transform.outputs['transform_graph'],
    #     schema=schema_gen.outputs['schema'],
    #     train_args=train_args,
    #     eval_args=eval_args)
    # components.append(trainer)

    # Trainer
    trainer_args = {
        'run_fn': run_fn,
        'transformed_examples': transform.outputs['transformed_examples'],
        'schema': schema_gen.outputs['schema'],
        'transform_graph': transform.outputs['transform_graph'],
        'train_args': train_args,
        'eval_args': eval_args,
        'custom_config': {
            tfx.extensions.google_cloud_ai_platform.ENABLE_VERTEX_KEY: True,
            tfx.extensions.google_cloud_ai_platform.VERTEX_REGION_KEY: train_cloud_region,
            tfx.extensions.google_cloud_ai_platform.TRAINING_ARGS_KEY: train_cloud_args,
            "use_gpu": True,
        },        
    }    
    trainer = tfx.extensions.google_cloud_ai_platform.Trainer(**trainer_args)
    components.append(trainer)

    model_resolver = resolver.Resolver(
        strategy_class=latest_blessed_model_resolver.LatestBlessedModelResolver,
        model=Channel(type=Model),
        model_blessing=Channel(
            type=ModelBlessing)).with_id('latest_blessed_model_resolver')
    components.append(model_resolver)

    # Uses TFMA to compute evaluation statistics over features of a model and
    # perform quality validation of a candidate model (compare to a baseline).
    eval_config = tfma.EvalConfig(
        model_specs=[tfma.ModelSpec(label_key='label_xf')],
        slicing_specs=[tfma.SlicingSpec()],
        metrics_specs=[
            tfma.MetricsSpec(metrics=[
                tfma.MetricConfig(
                    class_name='SparseCategoricalAccuracy',
                    threshold=tfma.MetricThreshold(
                        value_threshold=tfma.GenericValueThreshold(
                            lower_bound={'value': 0.55}),
                        # Change threshold will be ignored if there is no
                        # baseline model resolved from MLMD (first run).
                        change_threshold=tfma.GenericChangeThreshold(
                            direction=tfma.MetricDirection.HIGHER_IS_BETTER,
                            absolute={'value': -1e-3})))
            ])
        ])

    evaluator = Evaluator(
        examples=transform.outputs['transformed_examples'],
        model=trainer.outputs['model'],
        baseline_model=model_resolver.outputs['model'],
        eval_config=eval_config)
    components.append(evaluator)

    pusher = Pusher(
        model=trainer.outputs['model'],
        model_blessing=evaluator.outputs['blessing'],
        push_destination=pusher_pb2.PushDestination(
            filesystem=pusher_pb2.PushDestination.Filesystem(
                base_directory=serving_model_dir)))
    components.append(pusher)

    return pipeline.Pipeline(
        pipeline_name=pipeline_name,
        pipeline_root=pipeline_root,
        components=components,
        # Change this value to control caching of execution results. Default value
        # is `False`.
        enable_cache=True,
        metadata_connection_config=metadata_connection_config,
        beam_pipeline_args=beam_pipeline_args,
    )

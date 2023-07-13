import multiprocessing
from multiprocessing.pool import AsyncResult
import os
from typing import Optional, List, Tuple, Dict

from parsoda.model import ParsodaDriver, Crawler, Filter, Mapper, Reducer
from multiprocessing import Pool

from parsoda.model.function.crawler import CrawlerPartition

def _task_load(p: CrawlerPartition):
    return p.load_data().parse_data()

def _task_filter(filter_func, dataset: List):
    filtered_partition = []
    for item in dataset:
        if filter_func(item):
            filtered_partition.append(item)
    return filtered_partition

def _task_map(mapper, dataset: List):
    partition = []
    for item_list in map(mapper, dataset):
        partition.extend(item_list)
    return partition

def _task_sort(dataset: List, key=lambda kv: kv[0]):
    dataset.sort()
    return dataset

def _task_reduce(reducer, dataset: List[Tuple]):
    reduce_result = Dict()
    for kv in dataset:
        k, v = kv[0], kv[1]
        if k in reduce_result:
            reduce_result[k] = reducer(reduce_result[k], v)
        else:
            reduce_result[k] = v
    return reduce_result

class ParsodaMultiprocessingDriver(ParsodaDriver):

    def __init__(self, parallelism: int = -1):
        self.__parallelism = parallelism
        self.__dataset: Optional[list] = None
        self.__num_partitions = parallelism if parallelism>0 else multiprocessing.cpu_count()
        self.__pool: Pool = None

    def init_environment(self):
        self.__dataset = []
        self.__pool = Pool(self.__parallelism)

    def set_num_partitions(self, num_partitions):
        self.__num_partitions = num_partitions

    def crawl(self, crawlers: List[Crawler]):
        futures = []
        for crawler in crawlers:
            crawler_partitions = crawler.get_partitions(self.__num_partitions)
            for p in crawler_partitions:
                future: AsyncResult = self.__pool.apply_async(_task_load, (p))
                futures.append(future)

        for future in futures:
            self.__dataset.extend(future.get())

    @staticmethod
    def __define_partitions(dataset_size: int, num_partitions: int) -> List[Tuple[int, int]]:
        chunk_size = int(dataset_size / num_partitions)
        partitions = []
        item_index = 0
        while item_index < dataset_size:
            start = item_index
            end = int(min(start + chunk_size, dataset_size))
            item_index = item_index + chunk_size
            partitions.append((start, end))
        return partitions

    def filter(self, filter_func):
        filtered_items = []
        futures = []

        for start, end in self.__define_partitions(len(self.__dataset), self.__num_partitions):
            future = self.__pool.apply_async(_task_filter, (filter_func, self.__dataset[start:end]))
            futures.append(future)

        for future in futures:
            filtered_items.extend(future.get())
        self.__dataset = filtered_items

    def flatmap(self, mapper):
        mapped_items = []
        futures = []

        for start, end in self.__define_partitions(len(self.__dataset), self.__num_partitions):
            future = self.__pool.apply_async(_task_map, (mapper, self.__dataset[start:end]))
            futures.append(future)

        for future in futures:
            mapped_items.extend(future.get())
        self.__dataset = mapped_items

    def sort_by_key(self) -> None:
        sorted_items = []
        futures = []

        def merge(left: List, right: List, key=lambda kv: kv[0]):
            merged = []
            i, j = 0, 0
            while i < len(left) and j < len(right):
                if key(left[i]) <= key(right[j]):
                    merged.append(left[i])
                    i += 1
                else:
                    merged.append(right[j])
                    j += 1
            while i < len(left):
                merged.append(left[i])
                i += 1
            while j < len(right):
                merged.append(right[j])
                j += 1
            return merged

        for start, end in self.__define_partitions(len(self.__dataset), self.__num_partitions):
            future = self.__pool.apply_async(_task_sort, (self.__dataset[start:end]))
            futures.append(future)

        for future in futures:
            sorted_partition = future.result()
            sorted_items = merge(sorted_items, sorted_partition)
        self.__dataset = sorted_items

    def reduce_by_key(self, reducer):
        reduced_items = {}
        futures = []

        def combine(accumulator: Dict, to_combine: Dict):
            for k, v in to_combine.items():
                if k in accumulator:
                    accumulator[k] = reducer(accumulator[k], v)
                else:
                    accumulator[k] = v

        for start, end in self.__define_partitions(len(self.__dataset), self.__num_partitions):
            future = self.__pool.apply_async(_task_reduce, (reducer, self.__dataset[start:end]))
            futures.append(future)

        for future in futures:
            reduced_partition = future.result()
            combine(reduced_items, reduced_partition)

        self.__dataset = list(reduced_items.items())

    def get_result(self):
        return self.__dataset

    def dispose_environment(self):
        self.__dataset = None
        self.__pool.shutdown(wait=False)
        self.__pool = None
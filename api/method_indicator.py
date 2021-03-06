#!/usr/bin/env python
"""Indicators related functions."""
from database.operation import Operation
from method_data_source import MethodDataSource
from method_event import MethodEvent
from ast import literal_eval
from datetime import datetime
import logging
import os
import pandas
import api_utils

# Load logging configuration
log = logging.getLogger(__name__)


class MethodIndicator:
    """Functions called by the API for indicator objects."""

    def __init__(self, indicator_id):
        """Initialize class."""
        # Initialize dictionary for error message
        self.error_message = {}

        # Verify indicator exists
        indicator_list = Operation('ModelIndicator').read(id=indicator_id)
        if indicator_list:
            self.indicator = indicator_list[0]
        else:
            self.error_message['message'] = 'Indicator with Id {} does not exist'.format(indicator_id)
            log.error(self.error_message['message'])
            return self.error_message

    def execute(self, batch_id):
        """Execute a data quality indicator."""
        start_event = MethodEvent('Start').log_event(self.indicator.id, batch_id)
        session_id = start_event.sessionId

        # Get indicator parameters
        indicator_parameter_list = Operation('ModelIndicatorParameter').read(indicatorId=self.indicator.id)

        # Create dictionary from indicator parameters
        parameters = {}
        for indicator_parameter in indicator_parameter_list:
            parameters[indicator_parameter.parameterTypeId] = indicator_parameter.value

        # Verify parameters exist and convert them to list objects
        # Dimension parameter
        if 4 in parameters:
            parameters[4] = ['indicator_id'] + literal_eval(parameters[4])
        else:
            parameters[4] = ['indicator_id']

        # Measure parameter
        if 3 in parameters:
            parameters[3] = literal_eval(parameters[3])
        else:
            self.error_message['message'] = 'Indicator with Id {} does not have any measures parameter'.format(self.indicator.id)
            log.error(self.error_message['message'])
            return self.error_message

        # Get source and target data frames
        data_sets = {}
        for parameter in parameters:
            if parameter == 7:  # Source
                log.info('Getting data set from parameter Id {}, data source: {}'.format(parameter, parameters[parameter]))
                data_source_name = parameters[parameter]
                source_data_frame = MethodDataSource(data_source_name).get_data_frame(parameters[8])  # Source request
                source_data_frame.insert(loc=0, column='indicator_id', value=self.indicator.id)
                data_sets['Source data frame'] = source_data_frame

            elif parameter == 5:  # Target
                log.info('Getting data set from parameter Id {}, data source: {}'.format(parameter, parameters[parameter]))
                data_source_name = parameters[parameter]
                target_data_frame = MethodDataSource(data_source_name).get_data_frame(parameters[6])  # Target request
                target_data_frame.insert(loc=0, column='indicator_id', value=self.indicator.id)
                data_sets['Target data frame'] = target_data_frame

        # Verify data frames are not empty
        for data_frame_name in data_sets:
            if data_sets[data_frame_name].empty:
                log.error('{} is empty'.format(data_frame_name))

        # Format source and target data set with dimensions and measures parameters
        for data_frame_name in data_sets:
            log.info('Formatting {}'.format(data_frame_name))
            data_frame = data_sets[data_frame_name]
            column_name_list = parameters[4] + parameters[3]
            data_frame.columns = column_name_list
            for column in parameters[4]:
                data_frame[column] = data_frame[column].astype(str)
            data_sets[data_frame_name] = data_frame

        # Get indicator function and execute it
        indicator_type_list = Operation('ModelIndicatorType').read(id=self.indicator.indicatorTypeId)
        indicator_function = indicator_type_list[0].function
        result_data_frame = getattr(self, indicator_function)(data_sets, parameters)

        # Compute indicator result summary
        log.info('Computing result summary for indicator Id: {}'.format(self.indicator.id))
        nb_records_alert = self.compute_indicator_result(session_id, parameters, result_data_frame)

        # Send e-mail alert
        if 9 in parameters and not result_data_frame.loc[result_data_frame['Alert'] == True].empty:
            # Create csv file to send in attachment
            file_name = 'indicator_{}_session_{}.csv'.format(self.indicator.id, session_id)
            file_path = os.path.dirname(__file__) + "/" + file_name
            result_data_frame.to_csv(file_path, header=True, index=False)

            # Convert distribution list parameter to python list
            parameters[9] = literal_eval(parameters[9])

            # Prepare e-mail body
            body = {}
            body['indicator_name'] = self.indicator.name
            body['alert_threshold'] = parameters[1] + parameters[2]  # 'Alert operator' + Alert threshold
            body['nb_records_alert'] = nb_records_alert
            body['log_url'] = 'http://'  # To be updated

            # Send e-mail
            log.info('Sending e-mail alert for indicator Id {} and session Id {}'.format(self.indicator.id, session_id))
            api_utils.send_mail(
                template='indicator',
                distribution_list=parameters[9],
                attachment=file_path,
                **body)

            # Delete csv file
            os.remove(file_path)

        MethodEvent('Stop').log_event(self.indicator.id, batch_id)
        self.error_message['message'] = 'Indicator with Id {} completed successfully'.format(self.indicator.id)
        log.info(self.error_message['message'])
        return self.error_message

    def evaluate_completeness(self, data_sets, parameters):
        """Compute specificities of completeness indicator."""
        source_data_frame = data_sets['Source data frame']
        target_data_frame = data_sets['Target data frame']

        # Merge data frames to compare their measures
        result_data_frame = pandas.merge(
            left=source_data_frame,
            right=target_data_frame,
            left_on=parameters[4],  # Dimension
            right_on=parameters[4],  # Dimension
            how='outer',
            sort=True,
            suffixes=('_source', '_target'))
        result_data_frame = result_data_frame.fillna(value=0)  # Replace NaN values per 0

        # Prepare variables
        alert_operator = parameters[1]  # Alert operator
        alert_threshold = parameters[2]  # Alert threshold
        measure_list = parameters[3]  # Measure

        # Compute delta and delta percentage between source and target measures
        for measure in measure_list:
            source_column = measure + '_source'
            target_column = measure + '_target'
            delta_column = measure + '_delta'
            delta_percentage_column = measure + '_delta_percentage'

            # Compute delta
            delta = result_data_frame[target_column] - result_data_frame[source_column]
            result_data_frame[delta_column] = delta

            # Compute delta percentage
            result_data_frame[delta_percentage_column] = result_data_frame[delta_column] / result_data_frame[source_column]
            result_data_frame.loc[(result_data_frame[source_column] == 0), delta_percentage_column] = 1  # Replace delta percentage by 1 since can't divide by 0
            result_data_frame.loc[(result_data_frame[delta_column] == 0), delta_percentage_column] = 0  # Replace delta percentage by 0 since delta equal 0

            # Formatting data to improve readability
            result_data_frame[source_column] = round(result_data_frame[source_column], 2).astype(float)
            result_data_frame[target_column] = round(result_data_frame[target_column], 2).astype(float)
            result_data_frame[delta_column] = round(result_data_frame[delta_column], 2).astype(float)
            result_data_frame[delta_percentage_column] = round(result_data_frame[delta_percentage_column], 6).astype(float)

        # For each record and measure in data frame, test if alert must be sent and update alert column
        result_data_frame['Alert'] = False
        for measure in measure_list:
            for row_num in result_data_frame.index:
                measure_value = result_data_frame.loc[row_num, measure + '_delta_percentage']
                measure_value = abs(measure_value)*100
                if self.is_alert(measure_value, alert_operator, alert_threshold):
                    result_data_frame.loc[row_num, 'Alert'] = True
        return result_data_frame

    def evaluate_freshness(self, data_sets, parameters):
        """Compute specificities of freshness indicator."""
        result_data_frame = data_sets['Target data frame']
        result_data_frame['current_timestamp'] = datetime.utcnow()

        # Prepare variables
        alert_operator = parameters[1]  # Alert operator
        alert_threshold = parameters[2]  # Alert threshold
        measure_list = parameters[3]  # Measure

        # Compute delta in minutes and delta description between source and target measures
        for measure in measure_list:
            source_column = 'current_timestamp'
            target_column = measure
            delta_column = measure + '_delta_minutes'
            delta_description_column = measure + '_delta_description'

            # Enforce measure to datetime data type
            result_data_frame[source_column] = pandas.to_datetime(result_data_frame[source_column], format='%Y-%m-%d %H:%M:%S.%f', errors='ignore')
            result_data_frame[target_column] = pandas.to_datetime(result_data_frame[target_column], format='%Y-%m-%d %H:%M:%S.%f', errors='ignore')

            # Compute delta and delta description
            delta_seconds = (result_data_frame[source_column] - result_data_frame[target_column]).dt.total_seconds()
            result_data_frame[delta_column] = round(delta_seconds/60).astype(int)
            result_data_frame[delta_description_column] = pandas.to_timedelta(delta_seconds, unit='s')

        # For each record and measure in data frame, test if alert must be sent and update alert column
        result_data_frame['Alert'] = False
        for measure in measure_list:
            for row_num in result_data_frame.index:
                measure_value = result_data_frame.loc[row_num, measure + '_delta_minutes']
                measure_value = abs(measure_value)*100
                if self.is_alert(measure_value, alert_operator, alert_threshold):
                    result_data_frame.loc[row_num, 'Alert'] = True
        return result_data_frame

    def evaluate_latency(self, data_sets, parameters):
        """Compute specificities of latency indicator."""
        source_data_frame = data_sets['Source data frame']
        target_data_frame = data_sets['Target data frame']

        # Merge data frames to compare their measures
        result_data_frame = pandas.merge(
            left=source_data_frame,
            right=target_data_frame,
            left_on=parameters[4],  # Dimension
            right_on=parameters[4],  # Dimension
            how='outer',
            sort=True,
            suffixes=('_source', '_target'))

        # Prepare variables
        alert_operator = parameters[1]  # Alert operator
        alert_threshold = parameters[2]  # Alert threshold
        measure_list = parameters[3]  # Measure

        # Compute delta in minutes and delta description between source and target measures
        for measure in measure_list:
            source_column = measure + '_source'
            target_column = measure + '_target'
            delta_column = measure + '_delta_minutes'
            delta_description_column = measure + '_delta_description'

            # Enforce measure to datetime data type
            result_data_frame[source_column] = pandas.to_datetime(result_data_frame[source_column], format='%Y-%m-%d %H:%M:%S.%f', errors='ignore')
            result_data_frame[target_column] = pandas.to_datetime(result_data_frame[target_column], format='%Y-%m-%d %H:%M:%S.%f', errors='ignore')

            # Compute delta and delta description
            delta_seconds = (result_data_frame[source_column] - result_data_frame[target_column]).dt.total_seconds()
            result_data_frame[delta_column] = round(delta_seconds/60).astype(int)
            result_data_frame[delta_description_column] = pandas.to_timedelta(delta_seconds, unit='s')

        # For each record and measure in data frame, test if alert must be sent and update alert column
        result_data_frame['Alert'] = False
        for measure in measure_list:
            for row_num in result_data_frame.index:
                measure_value = result_data_frame.loc[row_num, measure + '_delta_minutes']
                measure_value = abs(measure_value)*100
                if self.is_alert(measure_value, alert_operator, alert_threshold):
                    result_data_frame.loc[row_num, 'Alert'] = True
        return result_data_frame

    def evaluate_validity(self, data_sets, parameters):
        """Compute specificities of validity indicator."""
        # No tranformation needed for this data frame
        result_data_frame = data_sets['Target data frame']

        # Prepare variables
        alert_operator = parameters[1]  # Alert operator
        alert_threshold = parameters[2]  # Alert threshold
        measure_list = parameters[3]  # Measure

        # Formatting data to improve readability
        for measure in measure_list:
            result_data_frame[measure] = round(result_data_frame[measure], 2).astype(float)

        # For each record and measure in data frame, test if alert must be sent and update alert column
        result_data_frame['Alert'] = False
        for measure in measure_list:
            for row_num in result_data_frame.index:
                measure_value = result_data_frame.loc[row_num, measure]
                if self.is_alert(measure_value, alert_operator, alert_threshold):
                    result_data_frame.loc[row_num, 'Alert'] = True
        return result_data_frame

    def compute_indicator_result(self, session_id, parameters, result_data_frame):
        """Compute aggregated results for the indicator."""
        alert_operator = parameters[1]  # Alert operator
        alert_threshold = parameters[2]  # Alert threshold
        nb_records = len(result_data_frame)
        nb_records_alert = len(result_data_frame.loc[result_data_frame['Alert'] == True])
        nb_records_no_alert = len(result_data_frame.loc[result_data_frame['Alert'] == False])

        # Insert result to database
        Operation('ModelIndicatorResult').create(
            indicatorId=self.indicator.id,
            sessionId=session_id,
            alertOperator=alert_operator,
            alertThreshold=alert_threshold,
            nbRecords=nb_records,
            nbRecordsAlert=nb_records_alert,
            nbRecordsNoAlert=nb_records_no_alert
        )
        return nb_records_alert

    def is_alert(self, measure_value, alert_operator, alert_threshold):
        """
        Compare measure to alert threshold based on the alert operator, return True if an alert must be sent, False otherwise.
        Supported alert operators are: ==, >, >=, <, <=, !=
        """
        if eval(str(measure_value) + alert_operator + str(alert_threshold)):
            return True
        else:
            return False

/* Contest Management System
 * Copyright © 2013 Luca Wehrstedt <luca.wehrstedt@gmail.com>
 * Copyright © 2013 William Di Luigi <williamdiluigi@gmail.com>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program. If not, see <http://www.gnu.org/licenses/>.
 */
'use strict';

angular.module('pws.notifications', [])
  .factory('notificationHub', [
      '$timeout',
      function($timeout) {
    return {
      createAlert: function(type, msg, secs) {
        var alert = $('<div class="alert alert-' + type + ' hyphenate' +
            ' alert-dismissable"><button type="button" class="close" ' +
            'data-dismiss="alert" aria-hidden="true">&times;</button>' +
            msg + '</div>').hide();
        $(".notifications").prepend(alert);
        alert.slideDown('fast');
        $timeout(function() {
          alert.animate({'right': '-260px'}, function() {
            $(this).remove();
          });
        }, Math.round(1000 * secs));
      },
    };
  }])
  .directive('notifications', [function() {
    return {
      restrict: 'E',
      template: '<div class="notifications"></div>',
    };
  }]);
